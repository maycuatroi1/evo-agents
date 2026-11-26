"""``evo-agents kg status``: the project's sources and graph as the hub holds them (its kg_status), whole or in a few
lines for session hooks (``--brief``)."""

from __future__ import annotations

from evo_agents.clikit import Arg, Result, Text
from evo_agents.kg.cli.base import WITH_PROJECT, KgCommand, KgContext, project_failed, shown
from evo_agents.kg.project import ProjectError


class KgStatus(KgCommand):
    name = "status"
    help = "coverage, freshness and held deletions per source, as the hub holds them"
    hub_errors = True
    arguments = (
        *WITH_PROJECT,
        Arg("--brief", action="store_true", help="a few lines, for session hooks"),
        Arg("--sink", help="sink whose clearance applies (default: the agent session's)"),
    )

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.hub.kg_remote import hub_status
        from evo_agents.kg.status import render_brief

        try:
            text, status = hub_status(ctx.args.project, ctx.args.sink)
        except ProjectError as exc:
            raise project_failed(exc) from exc
        return shown(ctx, status, Text(render_brief(status) if ctx.args.brief else text), 0 if status.get("ok") else 1)


STATUS = KgStatus()
