"""``evo-agents worker agent ask|forward|read``: the agent of a chat run works with the agents and the knowledge of
other projects (``evo_agents.hub.server.agent_asks``, ``evo_agents.hub.server.agent_reads``).

- ``agent ask HANDLE TEXT`` asks agent HANDLE, of another project, for work or knowledge of its project
  (``chat_runs.ASK_COMMAND``; POST /v1/worker/runs/{id}/agent/asks). The hub queues the run of HANDLE for the person at
  the origin of the chain, one hop further, and opens a wait of this run that the end of that run answers: the command
  notes the wait in the run's file of decisions, as ``worker wait`` does, and says to end the turn, so the run parks
  and holds no worker; the answer, or why the run asked failed, comes back in this session as the message that
  answers the wait. The brief flags (``--goal``, ``--output``, ``--bounds``, ``--done-when``, ``--ref``) head the prompt
  of the run asked; ``--reason`` says why an agent of an agent system asks an agent off its interacts edges, which the
  hub refuses without one. ``--priority`` (runs.PRIORITIES, medium unless given) is the priority of the run asked: a
  worker claims its queued runs by it, and a run asked of an agent whose nodes are all busy waits in that queue for a
  free slot.
- ``agent forward --run RUN`` posts the last answer of run RUN, of the same trace, verbatim into this run's thread
  (POST /v1/worker/runs/{id}/agent/forwards).
- ``agent read PROJECT TOOL [TEXT]`` reads the knowledge graph or the memories of another project that the person at
  the origin of the chain reads (``chat_runs.READ_COMMAND``; POST /v1/worker/runs/{id}/agent/reads). TOOL is
  kg-search, kg-context, memory-search or memory-get, as the evo-hub tool of the same name reads its own project. It
  prints what the hub lets into the run: the items the hub sink of the run's project clears, then one line on how many
  were left out and why.

Each reads the run from EVO_RUN_ID and EVO_WORKER_HOME, as ``worker chat`` does, refuses outside a chat run of this
worker (``chat_only``), and calls the hub with the worker's token. A refusal of the hub is one ``error:`` line on
stderr and exit status 1.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import TextIO

from evo_agents.clikit import Arg, Group, Result, Stderr, Text, returns_object
from evo_agents.hub.runs import PRIORITIES
from evo_agents.worker.cli.base import EXIT_FAILED, AgentCommand, AgentRunContext, failure, text_of
from evo_agents.worker.home import WorkerStateError
from evo_agents.worker.run_decisions import note_decision

ROUTE = "agent/asks"
FORWARD_ROUTE = "agent/forwards"
READ_ROUTE = "agent/reads"
BRIEF = (
    "goal",
    "output",
    "bounds",
    "done_when",
    "refs",
    "reason",
)  # the optional fields of an ask, as the hub names them
TOOLS = ("kg-search", "kg-context", "memory-search", "memory-get")
READ_KEYS = ("project", "tool", "text", "shown", "withheld", "why")


def chat_only(ctx: AgentRunContext, what: str) -> None:
    """Refuse, before anything else, outside a chat run of this worker: only the agent of a chat run ``what``."""
    kind = ctx.kind
    if kind != "chat":
        article = "an" if kind[:1] in "aeiou" else "a"
        raise WorkerStateError(
            f"run {ctx.run_id} is {article} {kind} run: only the agent of a chat run {what} with `evo-agents worker "
            f"{ctx.words}`"
        )
    ctx.config  # noqa: B018 - read now, as the refusals of a missing state come before the command's own
    ctx.token  # noqa: B018


def chat_failed(exc: Exception) -> int | None:
    """The exit status of a failure of a chat run's command: every refusal of the hub is 1, and the worker extra is
    named as ``worker chat`` names it."""
    return failure(exc, "chat", hub_failed=EXIT_FAILED)


def ask_payload(args, handle: str, body: str) -> dict:
    """The body of the ask: the handle and text, and the brief fields, reason and priority only when given."""
    payload: dict = {"handle": handle, "body": body}
    for name in (*BRIEF, "priority"):
        value = getattr(args, name, None)
        if value:
            payload[name] = value
    return payload


def read_body(args) -> dict:
    """The body of ``agent read``: the project, the tool and what it looks for."""
    tool = args.tool.replace("-", "_")
    body: dict = {"project": args.project, "tool": tool, "limit": args.limit}
    if getattr(args, "graph", None):
        if not tool.startswith("kg_"):
            raise WorkerStateError("--graph names a knowledge graph: kg-search and kg-context take it")
        body["graph"] = args.graph
    if tool == "memory_get":
        if not args.text or not args.text.isdigit():
            raise WorkerStateError("memory-get takes the memory's id as TEXT")
        body["memory_id"] = int(args.text)
    elif tool == "kg_context" and args.ids:
        body["ids"] = list(args.ids)
    elif not args.text:
        raise WorkerStateError(f"{args.tool} takes TEXT, what to look for")
    else:
        body["query"] = args.text
    return body


def read_json(answer: dict) -> dict:
    """What ``--json`` prints of the hub's answer: its READ_KEYS, each null when the hub left it out."""
    return {key: answer.get(key) for key in READ_KEYS}


@dataclass(frozen=True)
class OneLine(Result):
    """A result whose ``--json`` is one line of JSON, as ``agent read`` has always printed it."""

    def show(self, out: TextIO, as_json: bool) -> None:
        if as_json:
            print(json.dumps(self.data, ensure_ascii=False), file=out)
        else:
            self.view.write(out)


class AgentAsk(AgentCommand):
    name = "ask"
    words = "agent ask"
    help = "ask agent HANDLE of another project for work or knowledge of its project; ends in a wait for its answer"
    kinds = None  # any run's agent reaches it; ``chat_only`` refuses another kind than chat
    parser_options = {
        "description": "Ask agent HANDLE, of another project, for work or knowledge this run cannot reach. Its run "
        "works for the person at the origin of the chain, one hop further; this run waits for its answer: end your "
        "turn after it."
    }
    arguments = (
        Arg("handle", metavar="HANDLE", help="the agent asked, @ left out or not"),
        Arg("text", metavar="TEXT", help="what you ask, all the agent needs, at most 16 KiB (- for stdin)"),
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        brief = parser.add_argument_group(
            "brief", "heads the prompt of the run asked; give at least goal, output, done-when"
        )
        brief.add_argument("--goal", help="what the agent asked should reach")
        brief.add_argument("--output", help="what its answer should hold")
        brief.add_argument("--bounds", help="what it should not do or touch")
        brief.add_argument("--done-when", dest="done_when", help="when its work is finished")
        brief.add_argument(
            "--ref",
            dest="refs",
            action="append",
            metavar="REF",
            help="a ref it starts from (repo:path, a run, a URL, plan:<id>/step:<step> for a session of that step); "
            "repeat",
        )
        parser.add_argument(
            "--reason",
            help="why you ask an agent off your system's interacts edges; such an ask is refused without one",
        )
        parser.add_argument(
            "--priority",
            choices=PRIORITIES,
            help="the priority of the run asked, which a worker claims its queued runs by (default: medium)",
        )

    def run(self, ctx: AgentRunContext) -> Result:
        chat_only(ctx, "asks an agent of another project")
        handle = ctx.args.handle.removeprefix("@").lower()
        answer = ctx.in_chats(ROUTE, ask_payload(ctx.args, handle, text_of(ctx.args.text)))
        if answer.get("refusal"):
            return Result(answer, Stderr(Text(f"error: @{handle} took no work: {answer['refusal']}")), EXIT_FAILED)
        decision_id = note_decision(ctx.home, ctx.run_id, answer.get("decision_id"))
        return Result(
            answer,
            Text(
                f"Asked @{handle}: its run is #{answer.get('run_id')}, and wait #{decision_id} of this run is open until "
                "it ends. End your turn once no work that does not depend on the answer is left: the run waits without "
                f"the agent and holds no worker, and this session goes on with a message naming decision #{decision_id} "
                "when the answer comes."
            ),
        )

    def failed(self, exc: Exception) -> int | None:
        return chat_failed(exc)


class AgentForward(AgentCommand):
    name = "forward"
    words = "agent forward"
    help = "post the last answer of run RUN of your trace verbatim into this run's thread"
    kinds = None  # any run's agent reaches it; ``chat_only`` refuses another kind than chat
    parser_options = {
        "description": "Post the last answer of run RUN, a run of the same trace of your agent system that has ended, "
        "verbatim into this run's thread, files and interactive answers included: it shows as written by RUN's agent "
        "and forwarded by you."
    }
    arguments = (Arg("--run", type=int, required=True, metavar="RUN", help="the run whose answer is forwarded"),)

    def run(self, ctx: AgentRunContext) -> Result:
        chat_only(ctx, "forwards an answer")
        run = ctx.args.run
        answer = ctx.in_chats(FORWARD_ROUTE, {"run_id": run})
        if answer.get("refusal"):
            return Result(
                answer, Stderr(Text(f"error: run #{run} was not forwarded: {answer['refusal']}")), EXIT_FAILED
            )
        return Result(
            answer,
            Text(
                f"Forwarded the last answer of run #{run} verbatim into this thread (message "
                f"#{answer.get('message_id')})."
            ),
        )

    def failed(self, exc: Exception) -> int | None:
        return chat_failed(exc)


class AgentRead(AgentCommand):
    name = "read"
    words = "agent read"
    help = "read the knowledge graph or the memories of another project, within what this run's project lets in"
    kinds = None  # any run's agent reaches it; ``chat_only`` refuses another kind than chat
    parser_options = {
        "description": "Read the knowledge graph (kg-search, kg-context) or the memories (memory-search, memory-get) "
        "of another project that the person at the origin of the chain reads. The hub leaves out what the hub sink of "
        "this run's project does not clear and says how many items it left out and why."
    }
    arguments = (
        Arg("project", metavar="PROJECT", help="the project read, another than this run's"),
        Arg("tool", metavar="TOOL", choices=TOOLS, help="kg-search, kg-context, memory-search or memory-get"),
        Arg("text", metavar="TEXT", nargs="?", help="what to look for; for memory-get, the memory's id"),
        Arg("--id", dest="ids", action="append", help="kg-context: a node id, in place of TEXT (repeatable)"),
        Arg("--limit", type=int, default=10, help="kg-search, memory-search: at most this many (1 to 20)"),
        Arg(
            "--graph",
            metavar="NAME",
            help="kg-search, kg-context: only the sources of this named graph of PROJECT (`hub kg graph list`)",
        ),
    )
    output = returns_object(*READ_KEYS)
    json_help = "the hub's answer as JSON"

    def run(self, ctx: AgentRunContext) -> Result:
        chat_only(ctx, "reads another project")
        answer = ctx.in_chats(READ_ROUTE, read_body(ctx.args))
        return OneLine(read_json(answer), Text(answer.get("text") or ""))

    def failed(self, exc: Exception) -> int | None:
        return chat_failed(exc)


AGENT_ASK = AgentAsk()
AGENT_FORWARD = AgentForward()
AGENT_READ = AgentRead()
AGENT = Group(
    "agent",
    "inside a chat run: ask an agent of another project, or read that project",
    [AGENT_ASK, AGENT_FORWARD, AGENT_READ],
)
