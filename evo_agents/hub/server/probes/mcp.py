"""The probe tool of /mcp (``evo_agents.hub.server.mcp``): curator_probe_report gives a review run the weekly report of
the project's probes (``service.report.build``) as text and data."""

from __future__ import annotations

from datetime import date

from fastapi import Request

from evo_agents.hub import curator
from evo_agents.hub.server.forge.service.protection import need_reader
from evo_agents.hub.server.probes.models.report import DEFAULT_WEEKS
from evo_agents.hub.server.probes.service.report import answer_lines, build, unprobed_lines, week_lines
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import Principal


async def mcp_answer(request: Request, project: str, user: Principal, arguments: dict, *, sink: str | None):
    """(text, data) of the MCP tool curator_probe_report."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        newest = date.fromisoformat(arguments["week"]) if arguments.get("week") else None
        found = await build(conn, access, user, weeks=arguments.get("weeks", DEFAULT_WEEKS), newest=newest, sink=sink)
    cap = "no probes (probes.budget_usd is null)" if found.budget_usd is None else f"{curator.money(found.budget_usd)}"
    lines = [f"The probe report of project {project}: k {found.k}, a week's budget {cap}."]
    for week in found.weeks:
        lines += week_lines(week)
    lines += unprobed_lines(found)
    lines += answer_lines(found)
    data = {**found.model_dump(mode="json"), "summary": f"{len(found.weeks)} weeks"}
    return "\n".join(lines), data
