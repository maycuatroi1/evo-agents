"""The project a request about tasks is in: the plugin's effective config there (``settings``: the prefix of a task's
number, the statuses with their groups, the types, and the status ``on_steps_done`` moves a task to), whether it
enables srs too (``srs_enabled``), who may read and write its tasks (``reader``, ``writer``, ``opened``,
``writer_of``), the members a task may name (``members``), and the build of its knowledge graph a write queues
(``graph_changed``).

A config whose statuses or types repeat a key, or whose ``on_steps_done`` names no status, is a 409: an admin of the
project fixes it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.access import has_role
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server.plugin_configs import stored_stacks
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.tasks import DEFAULT_STATUSES, DEFAULT_TYPES
from evo_agents.hub.server.tasks.models.values import PLUGIN, SRS, Settings, Status, TasksError, Writer
from evo_agents.hub.server.tasks.repository import tasks as repository
from evo_agents.plugins import config as plugin_config


def _statuses(values) -> tuple[Status, ...]:
    return tuple(Status(item["key"], item["name"], item["group"]) for item in values or DEFAULT_STATUSES)


def _checked(project: str, statuses: tuple[Status, ...], types: tuple[str, ...], on_done: str | None) -> None:
    keys = [status.key for status in statuses]
    problems = []
    for what, names in (("statuses", keys), ("types", list(types))):
        repeated = sorted({name for name in names if names.count(name) > 1})
        if repeated:
            problems.append(f"{what} repeat {', '.join(repeated)}")
    if on_done is not None and on_done not in keys:
        problems.append(f"on_steps_done names {on_done}, which is no status")
    if problems:
        raise TasksError(409, f"the config of plugin {PLUGIN} in project {project}: {'; '.join(problems)}")


async def settings(conn: AsyncConnection, project_id: int, project: str) -> Settings:
    """The plugin's effective config in the project; 404 when the project does not enable the plugin, 409 when the
    config cannot be used."""
    row = await enabled_in(conn, PLUGIN, project_id)
    if row is None:
        raise TasksError(
            404, f"plugin {PLUGIN} is not enabled in project {project}: an admin of the project enables it"
        )
    stacks = await stored_stacks(conn, [row.id], project_id, None)
    merged = plugin_config.merge(row.manifest.get("config"), stacks.get(row.id, []))
    values: dict[str, Any] = {name: item.value for name, item in merged.fields.items() if not item.secret}
    statuses = _statuses(values.get("statuses"))
    types = tuple(values.get("types") or DEFAULT_TYPES)
    on_done = values.get("on_steps_done")
    _checked(project, statuses, types, on_done)
    return Settings(values.get("prefix") or "T", statuses, types, on_done)


async def srs_enabled(conn: AsyncConnection, project_id: int) -> bool:
    """Whether the project enables the plugin srs, whose items a task may link to."""
    return await enabled_in(conn, SRS, project_id) is not None


def reader(access: ProjectAccess) -> None:
    """Reading needs a grant on the project (a hub admin without one gets this far only)."""
    if access.role is None:
        raise TasksError(403, f"reading the tasks of project {access.name} needs a grant on it")


def writer(access: ProjectAccess, what: str) -> None:
    reader(access)
    if not has_role(access.role, "writer"):
        raise TasksError(403, f"{what} in project {access.name} needs the writer role; you are {access.role}")


def missing(access: ProjectAccess, ref: str) -> TasksError:
    return TasksError(404, f"no task {ref} in project {access.name} that you can see: see `evo-agents hub task list`")


async def opened(conn: AsyncConnection, user, project: str) -> tuple[ProjectAccess, Settings]:
    """The member's access to ``project``, which they read, and the plugin's config there."""
    access = await project_access(conn, user, project)
    reader(access)
    return access, await settings(conn, access.project_id, access.name)


def writer_of(access: ProjectAccess, user, sink: str | None, config: Settings, what: str) -> Writer:
    """``user`` as they write in the project, once they hold the writer role there."""
    writer(access, what)
    return Writer(access, user.user_id, config, sink)


async def members(conn: AsyncConnection, access: ProjectAccess, logins: set[str]) -> dict[str, int]:
    """login -> user id of each of ``logins`` who holds a grant on the project; 422 naming those who hold none."""
    if not logins:
        return {}
    found = await repository.members(conn, access.project_id, logins)
    if set(found) != logins:
        absent = ", ".join(sorted(logins - set(found)))
        raise TasksError(422, f"a task names members of project {access.name} only; no grant there: {absent}")
    return found


async def graph_changed(jobs, conn: AsyncConnection, access: ProjectAccess) -> None:
    """Queue a build of the project's knowledge graph, whose tickets are the tasks (``kg``)."""
    await queue_if_configured(jobs, conn, access.project_id, access.name)


__all__ = [
    "graph_changed",
    "members",
    "missing",
    "opened",
    "reader",
    "settings",
    "srs_enabled",
    "writer",
    "writer_of",
]
