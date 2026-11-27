"""The tools of the hub runtime (``hub_runtime``): what the agent of a chat run that runs on the hub does in place of
the worker's commands, each through the code the worker's route runs, in a transaction of its own that holds the run's
row. A member's task run, which has no chat, is offered TASK_NAMES alone, the tools that need no thread; ``call``
answers a call of a tool the turn was not offered as one it does not know.

- ``kg_search``: a search of the project's knowledge graph, the graph its agent system binds it to included
  (``kg.tool_result``), through the agent sink.
- ``chat_post``: a message of the agent in its thread or a chat it is in (``chats.service.agents.agent_message``),
  masked.
- ``interactive_answer``: an HTML answer the chat shows inside the message that carries it, with the semantics, the
  size limit, the CSP and the bridge of ``--interactive`` (``outputs.service.store.record_held``, then a message).
- ``agent_ask``: ask an agent of another project, with the brief template, the friction and the trace budget of
  ``evo-agents worker agent ask`` (``agent_asks.ask_agent``); the run then parks until the answer wakes it.
- ``agent_forward``: post the answer of a run it asked verbatim (``agent_asks.forward``).
- ``escalate``: ask the Orchestrator of its system (``orchestrator.escalate``): a thread in the system's ops chat at
  the hop of the run, and a wait the Orchestrator's answer wakes; the run then parks as for ``agent_ask``.
- ``orchestrator_answer``, ``orchestrator_reassign``, ``orchestrator_ask_owner``: the Orchestrator's actions in a
  thread the hub opened to it (``orchestrator_actions``), as ``evo-agents worker triage answer|reassign|ask-owner``.
- ``ops_action``: the Orchestrator's actions in an ops thread (``ops_actions``), as ``evo-agents worker ops ...``.
- ``work_set``: as the entry agent of its system, name the work of its trace (title, goal, done-when) and record its
  outcome (met or not_met, what is left), which the work card shows (``work_cards.set_work``), as ``evo-agents worker
  work set``.
- ``memory_write``: a memory of the project, under the rules of ``memories.put``.
- ``skill_read``: the SKILL.md of a skill the turn lists, or one file of it, read from its latest bundle in the blob
  store (``skills.read_bundle``), at most MAX_SKILL_FILE bytes. The turn lists the skills the run's owner reads, the
  global ones of an audience it is in and those of the run's project (``server.skills``), that the profile's skill axis
  allows (``allowed_skills``); a project skill hides a global one of the same name. A turn with no such skill is not
  offered the tool, and the tool refuses a skill it did not list. Chat runs and task runs both get it.

A tool that is refused answers the model with why, as the worker's command prints it; nothing raises past ``call``.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from evo_agents.hub import kg_snapshots, outputs, profiles, runs, system_context
from evo_agents.hub.blobs import BlobStoreUnavailable, blob_key
from evo_agents.hub.jobs import JobQueue
from evo_agents.hub.kg_graph import Handles
from evo_agents.hub.memory import AGENT_SINK
from evo_agents.hub.memory_files import memory_type
from evo_agents.hub.memory_places import HARNESS
from evo_agents.hub.server import kg, skills, work_cards, work_status_models
from evo_agents.hub.server.agents.models.asks import AgentAskIn
from evo_agents.hub.server.agents.service import asks as agent_asks
from evo_agents.hub.server.blobs import put_bytes
from evo_agents.hub.server.chats.models.agents import AgentMessageIn
from evo_agents.hub.server.chats.service import agents as chat_agents
from evo_agents.hub.server.mcp_run import hub_principal
from evo_agents.hub.server.memories import routes as memories
from evo_agents.hub.server.memories.models import Written
from evo_agents.hub.server.memories.service.inputs import MemoryIn
from evo_agents.hub.server.ops.models import actions as ops_models
from evo_agents.hub.server.ops.service import actions as ops_actions
from evo_agents.hub.server.orchestrator import models as orchestrator_models
from evo_agents.hub.server.orchestrator.service import actions as orchestrator_actions
from evo_agents.hub.server.orchestrator.service import threads as orchestrator
from evo_agents.hub.server.outputs.models import OutputIn
from evo_agents.hub.server.outputs.service import store as output_store
from evo_agents.hub.server.runs.service.plan_runs import held_row
from evo_agents.hub.server.security import WORKER
from evo_agents.hub.skills import MAX_BUNDLE, SKILL_FILE, BundleError, read_bundle

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from evo_agents.hub.blobs import BlobStore
    from evo_agents.hub.config import HubConfig

log = logging.getLogger(__name__)

MAX_RESULT_CHARS = 12_000  # what one tool's result hands back to the model
INTERACTIVE_NAME = "answer.html"
MAX_SKILL_FILE = 64 * 1024  # bytes of one file skill_read hands back; a larger one is refused
MAX_SKILL_DESCRIPTION = 500  # characters of a skill's description in the turn's prompt
MAX_SKILLS_LISTED = 100  # skills the turn's prompt names
MAX_FILES_LISTED = 50  # files of a skill skill_read names after its SKILL.md
# the kg_more handles and the snapshots of each data directory, as the app holds them
_GRAPHS: dict[Path, tuple[Handles, kg_snapshots.SnapshotCoordinator]] = {}


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
OPS_ACTIONS = {  # ops_action's actions: (the body, the action of ops_actions)
    "rerun": (ops_models.OpsRunIn, ops_actions.rerun),
    "redeliver": (ops_models.OpsRunIn, partial(ops_actions.rerun, redeliver=True)),
    "answer_wait": (ops_models.OpsWaitIn, ops_actions.answer_wait),
    "curator_resume": (ops_models.OpsReasonIn, ops_actions.curator_resume),
    "read": (ops_models.OpsReadIn, ops_actions.read),
    "finding": (ops_models.OpsFindingIn, ops_actions.finding),
    "priority": (ops_models.OpsPriorityIn, ops_actions.priority),
    "plan_dispatch": (ops_models.OpsPlanIn, ops_actions.plan_dispatch),
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
        "ops_action",
        "As the Orchestrator, in an ops thread the hub opened to you for a batch: rerun or redeliver a failed run of "
        "it (run_id), answer a stuck wait (decision_id, text), resume the Curator its breaker paused, mark a notice "
        "read (notification_id), write a finding for the Curator (lens, severity, title, body), give a queued run of "
        "the project another priority (run_id, level), or hand a stalled plan of the batch again to the default "
        "system's entry agent (plan_id).",
        {
            "action": {"type": "string", "enum": list(OPS_ACTIONS)},
            "reason": _TEXT,
            "run_id": {"type": "integer"},
            "decision_id": {"type": "integer"},
            "text": _TEXT,
            "notification_id": {"type": "integer"},
            "lens": _TEXT,
            "severity": {"type": "string", "enum": ["low", "medium", "high"]},
            "title": _TEXT,
            "body": _TEXT,
            "level": {"type": "string", "enum": list(runs.PRIORITIES)},
            "plan_id": _TEXT,
        },
        ["action", "reason"],
    ),
    _function(
        "work_set",
        "As the entry agent of your system, once a hand-off opened the work chat: name the work of this trace (title, "
        "goal, done-when) that its card shows, and before your final answer record its outcome (met or not_met "
        "against the done-when) with what is left to do when not met. Give at least one.",
        {
            "title": _TEXT,
            "goal": _TEXT,
            "done_when": _TEXT,
            "outcome": {"type": "string", "enum": list(work_status_models.OUTCOMES)},
            "left": {"type": "string", "description": "what is still to do when the done-when is not met"},
        },
        [],
    ),
    _function(
        "memory_write",
        "Write a memory of the project: a markdown file with frontmatter (name, description, type).",
        {"name": {"type": "string", "description": "the file name, ending in .md"}, "body": _TEXT},
        ["name", "body"],
    ),
]
SKILL_TOOL = _function(
    "skill_read",
    "Read a skill listed under # Skills: its SKILL.md, or one file of it that the SKILL.md names (a reference).",
    {
        "skill": {"type": "string", "description": "the skill's name, as listed"},
        "file": {"type": "string", "description": "a path inside the skill, e.g. references/tone.md; default SKILL.md"},
    },
    ["skill"],
)
TOOLS.append(SKILL_TOOL)
SKILL_READ = SKILL_TOOL["function"]["name"]
NAMES = tuple(tool["function"]["name"] for tool in TOOLS if tool is not SKILL_TOOL)
TASK_NAMES = ("kg_search", "memory_write")  # the tools of a task run's turn: none needs a chat thread


def offered(names: tuple[str, ...]) -> list[dict]:
    """The definitions of TOOLS a turn offered ``names`` sends the model."""
    return [tool for tool in TOOLS if tool["function"]["name"] in names]


@dataclass(frozen=True)
class AllowedSkill:
    """A skill a turn lists: its latest version, as ``skills._skills`` reads it."""

    name: str
    scope: str  # global or project
    version: int
    description: str
    sha256: str
    size: int


@dataclass
class Turn:
    """What the tools of one turn of chat run ``run_id`` of ``project`` reach."""

    engine: AsyncEngine
    config: HubConfig
    blobs: BlobStore
    sealer: Any  # a Sealer, None without EVO_HUB_SECRETS_KEY (app.state.sealer)
    run_id: int
    project: str
    names: tuple[str, ...] = NAMES  # the tools the turn is offered: NAMES for a chat run, TASK_NAMES for a task run
    asked: list[int] = field(default_factory=list)  # the runs it asked or escalated to this turn
    state: SimpleNamespace | None = None
    skills: tuple[AllowedSkill, ...] = ()  # the skills it lists, which skill_read reads (``allowed_skills``)
    bundles: dict[str, Any] = field(default_factory=dict)  # the bundles skill_read read this turn, by SHA-256

    def app_state(self) -> SimpleNamespace:
        """What ``kg`` and ``memories`` read of the app's state, made once a turn."""
        if self.state is None:
            data_dir = Path(self.config.data_dir)
            if data_dir not in _GRAPHS:
                _GRAPHS[data_dir] = (
                    Handles(),
                    kg_snapshots.SnapshotCoordinator(),  # no listener: a snapshot behind the newest build reloads
                )
            handles, snapshots = _GRAPHS[data_dir]
            snapshots.engine = self.engine
            self.state = SimpleNamespace(
                engine=self.engine,
                config=self.config,
                blobs=self.blobs,
                kg_handles=handles,
                kg_snapshots=snapshots,
                jobs=JobQueue.in_transaction(),
            )
        return self.state


async def allowed_skills(
    conn, config: HubConfig, run_id: int, project_id: int, permissions: dict | None
) -> tuple[AllowedSkill, ...]:
    """The skills a turn of run ``run_id`` lists: those its owner reads, the global ones of an audience the owner is in
    and those of project ``project_id``, that the skill axis of ``permissions`` allows; a project skill in place of a
    global one of the same name, ignoring case. Nothing when the owner holds no grant on the project any more."""
    try:
        user = await hub_principal(conn, run_id, WORKER)
    except HTTPException:
        return ()
    with_global = skills.AUDIENCES if config.is_admin(user.admin_login) else (skills.EVERYONE,)
    found: dict[str, AllowedSkill] = {}
    for row in (await conn.execute(skills.latest_skills(with_global, [project_id]))).all():
        if not profiles.allows(permissions, "skill", row.name):
            continue
        key = row.name.lower()
        if key in found and row.scope != "project":
            continue
        found[key] = AllowedSkill(row.name, row.scope, row.version, row.description, row.sha256, row.size)
    return tuple(sorted(found.values(), key=lambda skill: skill.name.lower()))


def skills_prompt(listed: tuple[AllowedSkill, ...]) -> str:
    """The section of a turn's system prompt naming the skills it lists; empty without one."""
    if not listed:
        return ""
    lines = [
        "# Skills",
        "",
        f"These skills hold instructions for tasks of this project. When one fits the work, read it with the tool "
        f"{SKILL_READ} before you act, then read the files its SKILL.md points to that you need.",
        "",
    ]
    for skill in listed[:MAX_SKILLS_LISTED]:
        description = " ".join(skill.description.split())
        if len(description) > MAX_SKILL_DESCRIPTION:
            description = description[:MAX_SKILL_DESCRIPTION].rstrip() + "..."
        lines.append(f"- {skill.name}: {description}")
    if len(listed) > MAX_SKILLS_LISTED:
        lines.append(f"- and {len(listed) - MAX_SKILLS_LISTED} more, read by name")
    return "\n".join(lines)


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
    body = AgentMessageIn(body=arguments.get("body") or "", chat_id=None, reply_to=arguments.get("reply_to"))
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
        body = AgentMessageIn(body=text, chat_id=None, reply_to=None, output_ids=[output_id])
        _, message_id = await chat_agents.agent_message(conn, turn.sealer, run, body)
    return f"sent the interactive answer as output #{output_id} in message #{message_id}"


def _ask_body(arguments: dict, handle: str) -> AgentAskIn:
    fields = ("body", *system_context.BRIEF_FIELDS, "refs", "reason")
    return AgentAskIn(handle=handle, **{key: arguments[key] for key in fields if arguments.get(key)})


async def _ask(turn: Turn, body: AgentAskIn, tool: str) -> str:
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
    body = orchestrator_models.EscalateIn(**{key: arguments[key] for key in fields if arguments.get(key)})
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
    body = orchestrator_models.OrchestratorAnswerIn(**arguments)
    async with turn.engine.begin() as conn:
        acted = await orchestrator_actions.answer(conn, turn.sealer, await _agent_run(conn, turn), body)
    return f"answered (run #{acted.run_id}); Decision line #{acted.message_id}"


async def orchestrator_reassign(turn: Turn, arguments: dict) -> str:
    body = orchestrator_models.OrchestratorReassignIn(
        **{**arguments, "handle": str(arguments.get("handle") or "").lstrip("@")}
    )
    async with turn.engine.begin() as conn:
        acted = await orchestrator_actions.reassign(conn, turn.sealer, await _agent_run(conn, turn), body)
    return f"gave the work to run #{acted.run_id}; Decision line #{acted.message_id}"


async def orchestrator_ask_owner(turn: Turn, arguments: dict) -> str:
    body = orchestrator_models.OrchestratorAskOwnerIn(**arguments)
    async with turn.engine.begin() as conn:
        run = await _agent_run(conn, turn)
        row = await held_row(conn, turn.run_id, None, lock=True, kinds=("chat",))
        acted = await orchestrator_actions.ask_owner(conn, run, body, row)
    return (
        f"asked the owner decision #{acted.decision_id}: end your turn now with a short reply that says so; the "
        "answer wakes this run in a new turn"
    )


async def work_set(turn: Turn, arguments: dict) -> str:
    body = work_cards.WorkSetIn(**{key: arguments[key] for key in work_cards.FIELDS if arguments.get(key)})
    async with turn.engine.begin() as conn:
        card = await work_cards.set_work(conn, await _agent_run(conn, turn), body)
    return f"the work of trace #{card.trace_id} is now: {card.title} (status {card.status})"


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
    memory = MemoryIn(
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
    if not isinstance(written, Written):
        return f"refused: {bytes(written.body).decode(errors='replace')}"
    return f"wrote memory {written.name} (revision {written.revision})"


def _fetched(store, skill: AllowedSkill):
    """The files of ``skill``'s bundle, read from the blob store and checked (blocking: run it in a thread)."""
    data = io.BytesIO()
    if store.fetch(blob_key(skill.sha256), data, MAX_BUNDLE) != (skill.sha256, skill.size):
        raise BundleError("the blob store does not hold its bundle as published")
    return read_bundle(data.getvalue(), skill.name)


async def skill_read(turn: Turn, arguments: dict) -> str:
    wanted = str(arguments.get("skill") or "")
    skill = next((item for item in turn.skills if item.name.lower() == wanted.lower()), None)
    if skill is None:
        return f"refused: no skill {wanted} for this agent; the skills are {', '.join(s.name for s in turn.skills)}"
    path = str(arguments.get("file") or SKILL_FILE).lstrip("/")
    if turn.blobs is None:
        return "error: the blob store is not configured on this hub"
    contents = turn.bundles.get(skill.sha256)
    if contents is None:
        try:
            contents = await asyncio.to_thread(_fetched, turn.blobs, skill)
        except BlobStoreUnavailable:
            return "error: the blob store did not answer; try again shortly"
        except BundleError as exc:
            log.error("a skill's bundle does not read", extra={"skill": skill.name, "sha256": skill.sha256})
            return f"error: skill {skill.name} does not read: {exc}"
        turn.bundles[skill.sha256] = contents
    entry = next((item for item in contents.files if item.path == path), None)
    if entry is None:
        return f"error: skill {skill.name} has no file {path}; its files are {_files(contents)}"
    if len(entry.data) > MAX_SKILL_FILE:
        return f"refused: {path} of skill {skill.name} is {len(entry.data)} bytes, over {MAX_SKILL_FILE}"
    try:
        text = entry.data.decode()
    except UnicodeDecodeError:
        return f"refused: {path} of skill {skill.name} is not text"
    if path == SKILL_FILE:
        text += f"\n\n[files of skill {skill.name}, version {skill.version}: {_files(contents)}]"
    return text


def _files(contents) -> str:
    paths = [entry.path for entry in contents.files]
    more = f" and {len(paths) - MAX_FILES_LISTED} more" if len(paths) > MAX_FILES_LISTED else ""
    return ", ".join(paths[:MAX_FILES_LISTED]) + more


async def ops_action(turn: Turn, arguments: dict) -> str:
    found = OPS_ACTIONS.get(str(arguments.pop("action", "")))
    if found is None:
        return f"error: action is one of {', '.join(OPS_ACTIONS)}"
    model, act = found
    body = model(**{key: value for key, value in arguments.items() if key in model.model_fields})
    async with turn.engine.begin() as conn:
        run = await _agent_run(conn, turn)
        await held_row(conn, turn.run_id, None, lock=True, kinds=("chat",))
        extra = {"public_url": turn.config.public_url} if act is ops_actions.plan_dispatch else {}
        acted = await act(conn, run, body, **extra)
    return f"done: the Decision line is message #{acted.message_id}; {acted.model_dump_json(exclude_none=True)}"


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
    "ops_action": ops_action,
    "work_set": work_set,
    "memory_write": memory_write,
    SKILL_READ: skill_read,
}


async def call(turn: Turn, name: str, raw: str) -> str:
    """The result of tool ``name`` with the JSON arguments ``raw``, for the model: a refusal or an error included."""
    handler = HANDLERS.get(name) if name in turn.names else None
    if handler is None:
        return f"error: no tool {name}; the tools are {', '.join(turn.names)}"
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
