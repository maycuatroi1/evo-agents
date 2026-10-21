"""One answer of the plugin, whoever asks: the facts of a project read for a reader (``reads``), the report or the
member's view computed from them (``report``), as the models of the API. The route and the tools both come here."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.pm import reads, report, settings
from evo_agents.core_plugins.pm.facts import Inputs
from evo_agents.core_plugins.pm.models import PmDaily, PmMember
from evo_agents.hub.server.projects import ProjectAccess


async def facts(
    conn: AsyncConnection, access: ProjectAccess, sink: str | None, day: date | None
) -> tuple[Inputs, date]:
    """The facts for the report of ``day`` (today in the project's time zone when None)."""
    reads.reader(access)
    config = await settings.config(conn, access.project_id, access.name)
    today = day or datetime.now(config.calendar.zone).date()
    return await reads.load(conn, access, sink, config, today), today


async def daily(conn: AsyncConnection, access: ProjectAccess, sink: str | None, day: date | None) -> PmDaily:
    data, today = await facts(conn, access, sink, day)
    return PmDaily.model_validate(report.daily(data, today))


async def member(
    conn: AsyncConnection, access: ProjectAccess, sink: str | None, day: date | None, login: str
) -> PmMember:
    data, today = await facts(conn, access, sink, day)
    if login not in {m.login for m in data.members}:
        raise settings.PmError(404, f"no member {login} with a grant on project {access.name}")
    return PmMember.model_validate(report.member(data, today, login))


__all__ = ["daily", "facts", "member"]
