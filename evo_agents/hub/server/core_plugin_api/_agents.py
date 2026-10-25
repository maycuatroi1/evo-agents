"""The plugin's agent types and chat runs (API 1.3): an agent type a project enabled, a task handed to it, and a reply
in the thread a chat run answers in. Private to ``core_plugin_api``."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server import agent_types, task_dispatch
from evo_agents.hub.server.chat_runs import hub_reply
from evo_agents.hub.server.core_plugin_api._reads import enabled_project
from evo_agents.hub.server.core_plugin_api._types import (
    AgentTypeRef,
    ChatRunRef,
    Plugin,
    TaskHanded,
    own_plugin,
)

MAX_REPLY_CHARS = 16_000


async def enabled_agent_type(conn: AsyncConnection, plugin: Plugin, project: str, name: str) -> AgentTypeRef | None:
    """The plugin's agent type ``name`` as ``project`` enabled it, or None when the project never did."""
    project_id = await enabled_project(conn, plugin, project)
    enabled = await enabled_in(conn, plugin.id, project_id)
    row = await agent_types.enabled_type(conn, project_id, enabled.id, name)
    if row is None:
        return None
    on = row.disabled_at is None and row.profile_deleted_at is None
    return AgentTypeRef(name, row.handle, row.profile_id, on)


async def hand_task(
    conn: AsyncConnection,
    plugin: Plugin,
    project: str,
    agent_type: str,
    task: str,
    *,
    title: str | None = None,
    for_user: int | None = None,
) -> TaskHanded:
    """Hand ``task`` to the plugin's agent type ``agent_type`` in ``project``, as the hub's task route does but for
    the token and the grant: a task run queued in the caller's transaction, for member ``for_user`` when given (the
    run is bounded by theirs and the type's run_as), or why none was."""
    project_id = await enabled_project(conn, plugin, project)
    if not isinstance(task, str) or not task.strip():
        raise ValueError("a task is a text that is not empty")
    run_id, refused = await task_dispatch.hand_own_task(
        conn, plugin.id, project_id, agent_type, task, title=title, for_id=for_user
    )
    return TaskHanded(run_id, refused)


async def reply_in_chat(conn: AsyncConnection, plugin: Plugin, run: ChatRunRef, text: str) -> int:
    """A message of the hub in the thread chat run ``run`` (from ``chat_run_of``) answers in, such as what a tool did
    outside the run; its id. ``text`` is cut to MAX_REPLY_CHARS."""
    own_plugin(plugin)
    if not isinstance(run, ChatRunRef):
        raise ValueError("a chat run comes from chat_run_of")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("a reply is a text that is not empty")
    return await hub_reply(conn, run.chat_id, run.root_id, text[:MAX_REPLY_CHARS])
