"""The plugin's effective config in a project (``Config``) and what pm reads of the config of the plugin tasks in it:
the groups of its statuses and the prefix of a task's number. The refusal every module raises is ``PmError``.

A project that does not enable tasks, or whose pm config names a time zone or working days that cannot be used, is a
409 with the reason: an admin of the project fixes it.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.pm.facts import Config
from evo_agents.core_plugins.pm.workdays import Calendar
from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server.plugin_configs import stored_stacks
from evo_agents.plugins import config as plugin_config

PLUGIN, TASKS, SRS = "pm", "tasks", "srs"
DEFAULT_TIMEZONE = "Asia/Ho_Chi_Minh"
DEFAULT_DAYS = (1, 2, 3, 4, 5)  # Monday to Friday, ISO
DEFAULT_STATUSES = (  # the statuses of the plugin tasks when its config names none
    ("backlog", "todo"),
    ("todo", "todo"),
    ("in_progress", "doing"),
    ("in_review", "review"),
    ("done", "done"),
    ("cancelled", "cancelled"),
)


class PmError(Exception):
    """Why the hub refuses a request about the daily report, with the HTTP status a route answers; nothing was
    written."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


async def _values(conn: AsyncConnection, row, project_id: int) -> dict:
    stacks = await stored_stacks(conn, [row.id], project_id, None)
    merged = plugin_config.merge(row.manifest.get("config"), stacks.get(row.id, []))
    return {name: item.value for name, item in merged.fields.items() if not item.secret}


def _zone(project: str, name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise PmError(
            409, f"the config of plugin {PLUGIN} in project {project}: timezone {name!r} is no IANA time zone"
        ) from None


def _days(project: str, given) -> frozenset[int]:
    days = frozenset(given or DEFAULT_DAYS)
    if not days or not all(isinstance(day, int) and 1 <= day <= 7 for day in days):
        raise PmError(409, f"the config of plugin {PLUGIN} in project {project}: working_days are ISO weekdays, 1 to 7")
    return days


async def config(conn: AsyncConnection, project_id: int, project: str) -> Config:
    """The plugin's effective config in the project; 404 when the project does not enable it, 409 when it cannot be
    used."""
    row = await enabled_in(conn, PLUGIN, project_id)
    if row is None:
        raise PmError(404, f"plugin {PLUGIN} is not enabled in project {project}: an admin of the project enables it")
    values = await _values(conn, row, project_id)
    calendar = Calendar(
        _zone(project, values.get("timezone") or DEFAULT_TIMEZONE), _days(project, values.get("working_days"))
    )
    return Config(
        calendar,
        stale_days=values.get("stale_days") or 2,
        escalate_after_days=1 if values.get("escalate_after_days") is None else values["escalate_after_days"],
        at_risk_days=2 if values.get("at_risk_days") is None else values["at_risk_days"],
        wip_limit=values.get("wip_limit") or 2,
        pm=values.get("pm") or None,
        digest_chat=values.get("digest_chat") or None,
    )


async def task_groups(conn: AsyncConnection, project_id: int, project: str) -> tuple[str, dict[str, str]]:
    """(the prefix of a task's number, status key -> group) as the plugin tasks has them in the project; 409 when the
    project does not enable tasks."""
    row = await enabled_in(conn, TASKS, project_id)
    if row is None:
        raise PmError(
            409,
            f"plugin {TASKS} is not enabled in project {project}: the daily report reads its tasks; an admin enables it",
        )
    values = await _values(conn, row, project_id)
    statuses = values.get("statuses")
    groups = {s["key"]: s["group"] for s in statuses} if statuses else dict(DEFAULT_STATUSES)
    return values.get("prefix") or "T", groups


async def srs_enabled(conn: AsyncConnection, project_id: int) -> bool:
    """Whether the project enables the plugin srs, whose use cases the report names."""
    return await enabled_in(conn, SRS, project_id) is not None


__all__ = ["PLUGIN", "PmError", "config", "srs_enabled", "task_groups"]
