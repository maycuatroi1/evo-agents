"""The tools of the hub runtime (``hub_runtime``): what the agent of a chat run that runs on the hub does in place of
the worker's commands, each through the code the worker's route runs, in a transaction of its own that holds the run's
row.

- ``kg_search``: a search of the project's knowledge graph, the graph its agent system binds it to included
  (``kg.tool_result``), through the agent sink.
- ``chat_post``: a message of the agent in its thread or a chat it is in (``chat_agents.agent_message``), masked.
- ``interactive_answer``: an HTML answer the chat shows inside the message that carries it, with the semantics, the
  size limit, the CSP and the bridge of ``--interactive`` (``output_store.record_held``, then a message).
- ``agent_ask``: ask an agent of another project, with the brief template, the friction and the trace budget of
  ``evo-agents worker agent ask`` (``agent_asks.ask_agent``); the run then parks until the answer wakes it.
- ``agent_forward``: post the answer of a run it asked verbatim (``agent_asks.forward``).
- ``escalate``: ask the Orchestrator of its system (``orchestrator.escalate``): a thread in the system's ops chat at
  the hop of the run, and a wait the Orchestrator's answer wakes; the run then parks as for ``agent_ask``.
- ``orchestrator_answer``, ``orchestrator_reassign``, ``orchestrator_ask_owner``: the Orchestrator's actions in a
  thread the hub opened to it (``orchestrator_actions``), as ``evo-agents worker triage answer|reassign|ask-owner``.
- ``memory_write``: a memory of the project, under the rules of ``memories.put``.

A tool that is refused answers the model with why, as the worker's command prints it; nothing raises past ``call``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from evo_agents.hub import outputs, system_context
from evo_agents.hub.jobs import JobQueue
from evo_agents.hub.kg_graph import GraphCache, Handles
from evo_agents.hub.memory import AGENT_SINK
from evo_agents.hub.memory_files import memory_type
from evo_agents.hub.memory_places import HARNESS
from evo_agents.hub.server import (
    agent_asks,
    chat_agents,
    kg,
    memories,
    orchestrator,
    orchestrator_actions,
    output_store,
)
from evo_agents.hub.server.blobs import put_bytes
from evo_agents.hub.server.mcp_run import hub_principal
from evo_agents.hub.server.output_store import OutputIn
from evo_agents.hub.server.runs.service.plan_runs import held_row
from evo_agents.hub.server.security import WORKER

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from evo_agents.hub.blobs import BlobStore
    from evo_agents.hub.config import HubConfig

log = logging.getLogger(__name__)

MAX_RESULT_CHARS = 12_000  # what one tool's result hands back to the model
INTERACTIVE_NAME = "answer.html"
# the cache of built graphs of each data directory, as the app holds one
_GRAPHS: dict[Path, tuple[GraphCache, Handles]] = {}


def _function(name: str, description: str, properties: dict, required: list[str]) -> dict:
    schema = {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
    return {"type": "function", "function": {"name": name, "description": description, "parameters": schema}}


_TEXT = {"type": "string"}
_BRIEF = {
    "goal": {"type": "string", "description": "what the agent asked should reach"},
    "output": {"type": "string", "description": "what its answer should hold"},
    "bounds": {"type": "string", "description": "what it should not do or touch"},
    "done_when": {"type": "string", "description": "when its work is finished"},
    "refs": {"type": "array", "items": _TEXT, "description": "refs it starts from: repo:path, a run, a URL"},
}
TOOLS = [
    _function(
        "kg_search",
        "Search the project's knowledge graph (repos, files, symbols, documents, plans).",
        {"query": _TEXT, "repo": {"type": "string", "description": "a repo of the project, to find code by name"}},
        ["query"],
    ),
    _function(
        "chat_post",
        "Post a message in the thread now, before the reply that ends the turn.",
        {"body": _TEXT, "reply_to": {"type": "integer", "description": "a message whose thread it goes in"}},
        ["body"],
    ),
    _function(
        "interactive_answer",
        "Send an interactive answer: one self-contained HTML file the chat shows inside the message.",
        {"html": _TEXT, "caption": _TEXT, "body": {"type": "string", "description": "the message that carries it"}},
        ["html"],
    ),
    _function(
        "agent_ask",
        "Ask an agent of another project; its answer wakes this run in a later turn.",
        {"handle": _TEXT, "body": _TEXT, **_BRIEF, "reason": {"type": "string", "description": "why, off design"}},
        ["handle", "body"],
    ),
    _function(
        "agent_forward",
        "Post the last answer of a run this run asked verbatim in the thread.",
        {"run_id": {"type": "integer"}},
        ["run_id"],
    ),
    _function(
        "escalate",
        "Hand the question to the Orchestrator of this agent's system; its answer wakes this run in a later turn.",
        {"question": _TEXT, **_BRIEF},
        ["question"],
    ),
    _function(
        "orchestrator_answer",
        "As the Orchestrator, in a thread the hub opened to you: answer it (wakes the agent that escalated).",
        {"text": _TEXT, "reason": _TEXT},
        ["text", "reason"],
    ),
    _function(
        "orchestrator_reassign",
        "As the Orchestrator, in a thread the hub opened to you: give its work to another agent node of the system.",
        {
            "handle": _TEXT,
            "note": {"type": "string", "description": "the work, as that agent reads it"},
            "reason": _TEXT,
        },
        ["handle", "note", "reason"],
    ),
    _function(
        "orchestrator_ask_owner",
        "As the Orchestrator, in a thread the hub opened to you: ask the person at the origin of the work a decision.",
        {
            "question": _TEXT,
            "context": _TEXT,
            "options": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"key": _TEXT, "label": _TEXT, "description": _TEXT},
                    "required": ["key", "label"],
                },
                "description": "2 to 6 options",
            },
            "reason": _TEXT,
        },
        ["question", "options", "reason"],
    ),
    _function(
        "memory_write",
        "Write a memory of the project: a markdown file with frontmatter (name, description, type).",
        {"name": {"type": "string", "description": "the file name, ending in .md"}, "body": _TEXT},
        ["name", "body"],
    ),
]
NAMES = tuple(tool["function"]["name"] for tool in TOOLS)


@dataclass
class Turn:
    """What the tools of one turn of chat run ``run_id`` of ``project`` reach."""

    engine: AsyncEngine
    config: HubConfig
    blobs: BlobStore
    sealer: Any  # a Sealer, None without EVO_HUB_SECRETS_KEY (app.state.sealer)
    run_id: int
    project: str
    asked: list[int] = field(default_factory=list)  # the runs it asked or escalated to this turn
    state: SimpleNamespace | None = None

    def app_state(self) -> SimpleNamespace:
        """What ``kg`` and ``memories`` read of the app's state, made once a turn."""
        if self.state is None:
            data_dir = Path(self.config.data_dir)
            if data_dir not in _GRAPHS:
                _GRAPHS[data_dir] = (GraphCache(data_dir / "kg" / "graphs"), Handles())
            graphs, handles = _GRAPHS[data_dir]
            self.state = SimpleNamespace(
                engine=self.engine,
                config=self.config,
                blobs=self.blobs,
                kg_graphs=graphs,
                kg_handles=handles,
                jobs=JobQueue.in_transaction(),
            )
        return self.state


def _cut(text: str) -> str:
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + "\n[... cut]"


async def _agent_run(conn, turn: Turn):
    return await chat_agents.held_run(conn, turn.run_id, None, None, WORKER)


async def kg_search(turn: Turn, arguments: dict) -> str:
    async with turn.engine.connect() as conn:
        user = await hub_principal(conn, turn.run_id, WORKER)
    wanted = {key: arguments[key] for key in ("query", "repo") if arguments.get(key)}
    result = await kg.tool_result(turn.app_state(), user, turn.project, "kg_search", wanted, AGENT_SINK)
    texts = [part.get("text", "") for part in result.get("content", []) if isinstance(part, dict)]
    return _cut("\n".join(texts) or json.dumps(result.get("structuredContent", {}), ensure_ascii=False))


async def chat_post(turn: Turn, arguments: dict) -> str:
    body = chat_agents.AgentMessageIn(
        body=arguments.get("body") or "", chat_id=None, reply_to=arguments.get("reply_to")
    )
    async with turn.engine.begin() as conn:
        run = await _agent_run(conn, turn)
        chat, message_id = await chat_agents.agent_message(conn, turn.sealer, run, body)
    return f"posted message #{message_id} in chat {chat.chat_id}"


async def interactive_answer(turn: Turn, arguments: dict) -> str:
    data = (arguments.get("html") or "").encode()
    sha = hashlib.sha256(data).hexdigest()
    item = OutputIn(
        sha256=sha,
        size=len(data),
        source=outputs.SENT,
        name=INTERACTIVE_NAME,
        media_type="text/html",
        caption=arguments.get("caption") or None,
        interactive=True,
        tool_call_id=None,
        upload_id=None,
    )
    await asyncio.to_thread(put_bytes, turn.blobs, sha, data)
    async with turn.engine.begin() as conn:
        run = await _agent_run(conn, turn)
        target = await output_store.target_of(conn, turn.run_id, turn.project)
        output_id = await output_store.record_held(conn, turn.blobs, target, run.principal.user_id, item, data)
        text = arguments.get("body") or arguments.get("caption") or "An interactive answer."
        body = chat_agents.AgentMessageIn(body=text, chat_id=None, reply_to=None, output_ids=[output_id])
        _, message_id = await chat_agents.agent_message(conn, turn.sealer, run, body)
    return f"sent the interactive answer as output #{output_id} in message #{message_id}"


def _ask_body(arguments: dict, handle: str) -> agent_asks.AgentAskIn:
    fields = ("body", *system_context.BRIEF_FIELDS, "refs", "reason")
    return agent_asks.AgentAskIn(handle=handle, **{key: arguments[key] for key in fields if arguments.get(key)})


async def _ask(turn: Turn, body: agent_asks.AgentAskIn, tool: str) -> str:
    async with turn.engine.begin() as conn:
        run = await _agent_run(conn, turn)
        row = await held_row(conn, turn.run_id, None, lock=True, kinds=("chat",))
        asked = await agent_asks.ask_agent(conn, turn.sealer, run, body, row, tool=tool)
    if asked.refusal is not None:
        return f"refused: {asked.refusal}"
    assert asked.run_id is not None  # ask_agent made the run or gave its refusal
    turn.asked.append(asked.run_id)
    return (
        f"asked @{body.handle} in run #{asked.run_id}: end your turn now with a short reply that says so; its answer "
        "wakes this run in a new turn"
    )


async def agent_ask(turn: Turn, arguments: dict) -> str:
    return await _ask(
        turn, _ask_body(arguments, str(arguments.get("handle") or "").lstrip("@")), system_context.TOOL_ASK
    )


async def escalate(turn: Turn, arguments: dict) -> str:
    fields = ("question", *system_context.BRIEF_FIELDS, "refs")
    body = orchestrator.EscalateIn(**{key: arguments[key] for key in fields if arguments.get(key)})
    async with turn.engine.begin() as conn:
        run = await _agent_run(conn, turn)
        row = await held_row(conn, turn.run_id, None, lock=True, kinds=("chat",))
        escalated = await orchestrator.escalate(conn, turn.sealer, run, body, row)
    if escalated.refusal is not None:
        return f"refused: {escalated.refusal}"
    assert escalated.run_id is not None  # escalate made the run or gave its refusal
    turn.asked.append(escalated.run_id)
    return (
        f"escalated to the Orchestrator in run #{escalated.run_id}: end your turn now with a short reply that says so; "
        "its answer wakes this run in a new turn"
    )


async def orchestrator_answer(turn: Turn, arguments: dict) -> str:
    body = orchestrator_actions.OrchestratorAnswerIn(**arguments)
    async with turn.engine.begin() as conn:
        acted = await orchestrator_actions.answer(conn, turn.sealer, await _agent_run(conn, turn), body)
    return f"answered (run #{acted.run_id}); Decision line #{acted.message_id}"


async def orchestrator_reassign(turn: Turn, arguments: dict) -> str:
    body = orchestrator_actions.OrchestratorReassignIn(
        **{**arguments, "handle": str(arguments.get("handle") or "").lstrip("@")}
    )
    async with turn.engine.begin() as conn:
        acted = await orchestrator_actions.reassign(conn, turn.sealer, await _agent_run(conn, turn), body)
    return f"gave the work to run #{acted.run_id}; Decision line #{acted.message_id}"


async def orchestrator_ask_owner(turn: Turn, arguments: dict) -> str:
    body = orchestrator_actions.OrchestratorAskOwnerIn(**arguments)
    async with turn.engine.begin() as conn:
        run = await _agent_run(conn, turn)
        row = await held_row(conn, turn.run_id, None, lock=True, kinds=("chat",))
        acted = await orchestrator_actions.ask_owner(conn, run, body, row)
    return (
        f"asked the owner decision #{acted.decision_id}: end your turn now with a short reply that says so; the "
        "answer wakes this run in a new turn"
    )


async def agent_forward(turn: Turn, arguments: dict) -> str:
    async with turn.engine.begin() as conn:
        run = await _agent_run(conn, turn)
        message_id, refusal = await agent_asks.forward(conn, run, int(arguments.get("run_id") or 0))
    return f"refused: {refusal}" if refusal else f"forwarded as message #{message_id}"


def _request(turn: Turn) -> Request:
    """The request ``memories.put`` reads the app's state of."""
    app = SimpleNamespace(state=turn.app_state())
    return Request({"type": "http", "app": app, "state": {}, "headers": [], "method": "POST", "path": "/"})


async def memory_write(turn: Turn, arguments: dict) -> str:
    text = arguments.get("body") or ""
    memory = memories.MemoryIn(
        scope="project",
        project=turn.project,
        location=HARNESS,
        name=arguments.get("name") or "",
        type=memory_type(text),
        body=text,
        label=None,
        if_revision=None,
        audience=None,
        agent=None,
    )
    async with turn.engine.connect() as conn:
        user = await hub_principal(conn, turn.run_id, WORKER)
    written = await memories.put(_request(turn), memory, user, sink=AGENT_SINK)
    if not isinstance(written, memories.Written):
        return f"refused: {bytes(written.body).decode(errors='replace')}"
    return f"wrote memory {written.name} (revision {written.revision})"


HANDLERS = {
    "kg_search": kg_search,
    "chat_post": chat_post,
    "interactive_answer": interactive_answer,
    "agent_ask": agent_ask,
    "agent_forward": agent_forward,
    "escalate": escalate,
    "orchestrator_answer": orchestrator_answer,
    "orchestrator_reassign": orchestrator_reassign,
    "orchestrator_ask_owner": orchestrator_ask_owner,
    "memory_write": memory_write,
}


async def call(turn: Turn, name: str, raw: str) -> str:
    """The result of tool ``name`` with the JSON arguments ``raw``, for the model: a refusal or an error included."""
    handler = HANDLERS.get(name)
    if handler is None:
        return f"error: no tool {name}; the tools are {', '.join(NAMES)}"
    try:
        arguments = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return "error: the arguments are not JSON"
    if not isinstance(arguments, dict):
        return "error: the arguments are not a JSON object"
    try:
        return await handler(turn, arguments)
    except HTTPException as exc:
        return f"refused: {exc.detail}"
    except (ValidationError, ValueError) as exc:
        return f"error: {_cut(str(exc))}"
