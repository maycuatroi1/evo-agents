"""The tasks of a project an isolated plugin keeps (``routes.tasks``), granted ``tasks:write`` in a project that has
the feature tasks on. Each call runs the code of a member's request (``evo_agents.hub.server.tasks``) in the name of the
admin who granted the plugin its permissions (``service.access.acting``), who is the author of the task, of each
change in its history and of each comment. A task the plugin makes carries the project's label, and its external key
is ``<plugin>:<key>`` (tasks_items.external_key, unique in the project), so a plugin finds and changes only the tasks
it made, and never a task an import or a member made; its ``external_ref`` names the plugin and the key. Creating or
changing a task queues a build of the project's knowledge graph, as a member's request does."""

from __future__ import annotations

import logging

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.plugin_api.models.access import Acting
from evo_agents.hub.server.plugin_api.models.tasks import FEATURE, PERMISSION, PluginTask, PluginTaskIn
from evo_agents.hub.server.plugin_api.repository import tasks as store
from evo_agents.hub.server.plugin_api.service.access import acting, feature_on, granted
from evo_agents.hub.server.task_dispatch import plugin_of_request
from evo_agents.hub.server.tasks.models.api import TasksComment, TasksCommentIn, TasksItemEdit, comment_model
from evo_agents.hub.server.tasks.models.values import (
    LinksIn,
    Settings,
    TaskChanges,
    TaskInput,
    TasksError,
    Writer,
    number_of,
)
from evo_agents.hub.server.tasks.repository import history
from evo_agents.hub.server.tasks.service import reads, writes
from evo_agents.hub.server.tasks.service.project import settings
from evo_agents.hub.server.tasks.service.project import writer as writer_role

log = logging.getLogger(__name__)


# Who acts, and on which task


async def _acting(conn: AsyncConnection, plugin_id: int, project: str) -> tuple[Acting, Settings]:
    enabled = await granted(conn, plugin_id, project, PERMISSION)
    await feature_on(conn, FEATURE, enabled)
    who = await acting(conn, enabled)
    return who, await settings(conn, who.access.project_id, who.access.name)


def _stored(who: Acting, key: str) -> str:
    return f"{who.plugin}:{key}"


def _writer(who: Acting, config: Settings) -> Writer:
    writer_role(who.access, f"plugin {who.plugin} writing a task")
    return Writer(who.access, who.user_id, config, None)


async def _own(conn: AsyncConnection, who: Acting, ref: str) -> tuple[int, str]:
    """(number, key) of task ``ref`` when the plugin made it; 404 for any other task of the project, or none."""
    number = number_of(ref)
    stored = await store.external_key(conn, who.access.project_id, number)
    prefix = _stored(who, "")
    if stored is None or not stored.startswith(prefix):
        raise TasksError(404, f"plugin {who.plugin} made no task {ref} in project {who.access.name}")
    return number, stored[len(prefix) :]


async def _shown(conn: AsyncConnection, who: Acting, config: Settings, number: int, key: str) -> PluginTask:
    found = await reads.task_detail(conn, who.access, config, number, None)
    return PluginTask(**found.model_dump(), key=key)


async def _graph_changed(jobs, conn: AsyncConnection, who: Acting) -> None:
    await queue_if_configured(jobs, conn, who.access.project_id, who.access.name)


# What each route does


async def create(engine: AsyncEngine, request: Request, jobs, project: str, body: PluginTaskIn) -> PluginTask:
    """A new task of the plugin under ``body.key``; ``TasksError`` 409 when the plugin made one under it already."""
    async with UnitOfWork(engine) as conn:
        plugin_id = await plugin_of_request(request, conn)
        who, config = await _acting(conn, plugin_id, project)
        writer = _writer(who, config)
        stored = _stored(who, body.key)
        held = await store.number_by_key(conn, who.access.project_id, stored)
        if held is not None:
            raise TasksError(409, f"plugin {who.plugin} made task {config.ref(held)} under key {body.key} already")
        task = TaskInput(
            title=body.title,
            description_md=body.description_md,
            type=body.type,
            status=body.status,
            priority=body.priority,
            assignees=body.assignees,
            roles=body.roles.given(),
            links=LinksIn(**body.links.model_dump()),
            external_key=stored,
            external_ref={"plugin": who.plugin, "key": body.key},
        )
        number = await writes.create(conn, writer, task)
        await _graph_changed(jobs, conn, who)
        log.info("plugin task created", extra={"project": project, "plugin": who.plugin, "task": config.ref(number)})
        return await _shown(conn, who, config, number, body.key)


async def find(engine: AsyncEngine, request: Request, project: str, key: str) -> list[PluginTask]:
    """The task the plugin made under ``key``: a list of one, or empty."""
    async with UnitOfWork(engine) as conn:
        plugin_id = await plugin_of_request(request, conn)
        who, config = await _acting(conn, plugin_id, project)
        number = await store.number_by_key(conn, who.access.project_id, _stored(who, key))
        return [] if number is None else [await _shown(conn, who, config, number, key)]


async def edit(engine: AsyncEngine, request: Request, jobs, project: str, ref: str, body: TasksItemEdit) -> PluginTask:
    """Change a task the plugin made: its fields, its status, its people, its links."""
    async with UnitOfWork(engine) as conn:
        plugin_id = await plugin_of_request(request, conn)
        who, config = await _acting(conn, plugin_id, project)
        writer = _writer(who, config)
        number, key = await _own(conn, who, ref)
        row = await reads.task(conn, who.access, number, None, lock=True)
        changes = TaskChanges(
            values=body.values(),
            assignees=body.assignees,
            roles=body.roles.given(),
            links=LinksIn(**body.links.model_dump()),
            unlink=body.unlink,
        )
        await writes.edit(conn, writer, row, changes)
        await _graph_changed(jobs, conn, who)
        return await _shown(conn, who, config, number, key)


async def comment(engine: AsyncEngine, request: Request, project: str, ref: str, body: TasksCommentIn) -> TasksComment:
    """Comment on a task the plugin made."""
    async with UnitOfWork(engine) as conn:
        plugin_id = await plugin_of_request(request, conn)
        who, config = await _acting(conn, plugin_id, project)
        writer = _writer(who, config)
        number, _ = await _own(conn, who, ref)
        row = await reads.task(conn, who.access, number, None)
        comment_id = await writes.comment(conn, writer, row.id, body.body)
        return comment_model(next(c for c in await history.comments(conn, row.id) if c.id == comment_id))


__all__ = ["comment", "create", "edit", "find"]
