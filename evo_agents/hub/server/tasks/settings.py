"""The plugin's effective config in a project (``Settings``): the prefix of a task's number, the statuses with their
groups, the types, and the status ``on_steps_done`` moves a task to; and the refusal every module raises
(``TasksError``). A config whose statuses or types repeat a key, or whose ``on_steps_done`` names no status, is a 409:
an admin of the project fixes it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server.plugin_configs import stored_stacks
from evo_agents.hub.server.tasks import DEFAULT_STATUSES, DEFAULT_TYPES, FINISHED
from evo_agents.plugins import config as plugin_config

PLUGIN = "tasks"
SRS = "srs"  # the plugin whose items a task links to, while the project enables it
REF = re.compile(r"^(?:([A-Za-z][A-Za-z0-9]{0,9})-)?([1-9][0-9]{0,8})$")  # T-42, or 42


class TasksError(Exception):
    """Why the hub refuses a request about tasks, with the HTTP status a route answers and any detail (the problems
    of an import); nothing was written."""

    def __init__(self, status: int, message: str, detail: list | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail


@dataclass(frozen=True)
class Status:
    key: str
    name: str
    group: str


@dataclass(frozen=True)
class Settings:
    """The plugin's effective config in a project."""

    prefix: str
    statuses: tuple[Status, ...]
    types: tuple[str, ...]
    on_steps_done: str | None = None

    def status(self, key: str | None) -> Status | None:
        return next((status for status in self.statuses if status.key == key), None)

    def group(self, key: str | None) -> str | None:
        found = self.status(key)
        return found.group if found else None

    def finished(self, key: str | None) -> bool:
        return self.group(key) in FINISHED

    def ref(self, number: int) -> str:
        """A task's number as shown: T-42."""
        return f"{self.prefix}-{number}"


def number_of(ref: str) -> int:
    """The number a task is named by: ``T-42`` (any prefix, the project's or one it had) or ``42``; 404 otherwise."""
    found = REF.fullmatch(ref.strip())
    if found is None:
        raise TasksError(404, f"{ref!r} names no task: give its number, as T-42 or 42")
    return int(found[2])


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
    values = {name: item.value for name, item in merged.fields.items() if not item.secret}
    statuses = _statuses(values.get("statuses"))
    types = tuple(values.get("types") or DEFAULT_TYPES)
    on_done = values.get("on_steps_done")
    _checked(project, statuses, types, on_done)
    return Settings(values.get("prefix") or "T", statuses, types, on_done)


async def srs_enabled(conn: AsyncConnection, project_id: int) -> bool:
    """Whether the project enables the plugin srs, whose items a task may link to."""
    return await enabled_in(conn, SRS, project_id) is not None


__all__ = ["PLUGIN", "SRS", "Settings", "Status", "TasksError", "number_of", "settings", "srs_enabled"]
