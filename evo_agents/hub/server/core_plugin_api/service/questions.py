"""What a core plugin asks its own agent types through the API (version 1.2): ``ask_agent_type`` queues a read-only
question run of an agent type the plugin contributes, for a person outside the hub, and ``read_agent_answer`` reads
what it answered; the plugin also gets ``ANSWER_EVENT`` when the run ends (``docs/plugins.md``, Agent types). The code
is ``evo_agents.hub.server.question_dispatch``. Private to ``core_plugin_api``."""

from __future__ import annotations

from types import MappingProxyType

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server import question_dispatch
from evo_agents.hub.server.core_plugin_api.models.questions import AgentAnswer, AgentQuestion
from evo_agents.hub.server.core_plugin_api.models.types import Clearance, Plugin
from evo_agents.hub.server.core_plugin_api.service.handles import own_clearance, own_plugin, refusing


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
