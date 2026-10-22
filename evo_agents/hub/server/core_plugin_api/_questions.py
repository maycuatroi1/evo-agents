"""What a core plugin asks its own agent types through the API (version 1.2): ``ask_agent_type`` queues a read-only
question run of an agent type the plugin contributes, for a person outside the hub, and ``read_agent_answer`` reads
what it answered; the plugin also gets ``ANSWER_EVENT`` when the run ends (``docs/plugins.md``, Agent types). The code
is ``evo_agents.hub.server.question_dispatch``. Private to ``core_plugin_api``."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import question_runs
from evo_agents.hub.server import question_dispatch
from evo_agents.hub.server.core_plugin_api._types import Clearance, Plugin, own_clearance, own_plugin, refusing

ANSWER_EVENT: str = question_runs.ANSWER_EVENT
MAX_QUESTION_BYTES: int = question_runs.MAX_QUESTION_BYTES
MAX_ASKER_CHARS: int = question_runs.MAX_ASKER_CHARS
MAX_REF_CHARS: int = question_runs.MAX_REF_CHARS
MAX_ANSWER_CHARS: int = question_runs.MAX_ANSWER_CHARS


@dataclass(frozen=True)
class AgentQuestion:
    """A question of the plugin to its agent type ``agent_type`` of ``project``, its run ``run_id``; ``made`` is False
    when ``ref`` named this question before and nothing was asked now."""

    question_id: int
    run_id: int
    project: str
    agent_type: str
    ref: str
    made: bool


@dataclass(frozen=True)
class AgentAnswer:
    """What the plugin reads of a question: ``pending`` until its run ended, then done (``text`` the answer), failed
    (``failure_cause`` why), cancelled, or refused when the project is kept from the place the answer goes, read again
    each time; ``label`` is the project's default label, which the answer carries."""

    question_id: int
    ref: str
    project: str
    agent_type: str
    run_id: int
    state: Literal["pending", "done", "failed", "cancelled", "refused"]
    text: str | None
    label: Mapping[str, Any]
    failure_cause: str | None


async def ask_agent_type(
    conn: AsyncConnection,
    plugin: Plugin,
    project: str,
    agent_type: str,
    question: str,
    *,
    asker: str,
    ref: str,
    cleared: Clearance,
) -> AgentQuestion:
    """Ask agent type ``agent_type`` of the plugin in ``project`` ``question`` for ``asker``, a person outside the hub
    the plugin names, under the plugin's key ``ref`` (one per project: asking again with it gives that question back,
    ``made`` False), the answer to go to a place cleared for ``cleared``; in the plugin's transaction. Refused: 422 a
    bad question, asker or ref, 404 a project that did not enable the plugin or the type, 409 a type whose profile or
    run_as cannot run or with no worker that can, 403 a project kept from ``cleared``, 429 the type's cap reached."""
    found = own_plugin(plugin)
    where = own_clearance(cleared)
    with refusing():
        made = await question_dispatch.ask(
            conn,
            found.id,
            project,
            agent_type,
            question,
            asker=asker,
            ref=ref,
            cleared=(where.level, where.location, where.below),
        )
    return AgentQuestion(**made)


async def read_agent_answer(conn: AsyncConnection, plugin: Plugin, question_id: int) -> AgentAnswer:
    """The answer of question ``question_id`` of the plugin, as it is now; 404 for a question of another plugin and
    for none."""
    found = own_plugin(plugin)
    if isinstance(question_id, bool) or not isinstance(question_id, int):
        raise ValueError("a question id is an int, the question_id of an AgentQuestion")
    with refusing():
        answer = await question_dispatch.answer_of(conn, found.id, question_id)
    return AgentAnswer(**{**answer, "label": MappingProxyType(dict(answer["label"]))})
