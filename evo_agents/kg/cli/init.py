"""``evo-agents kg init``: ask the hub for a kg_init run of the project, in which the Curator configures its graph from
its data profile (``evo-agents hub kg init``); the run, or the one already in flight."""

from __future__ import annotations

from evo_agents.clikit import Result, Text
from evo_agents.kg.cli.base import WITH_PROJECT, KgCommand, KgContext, shown


class KgInit(KgCommand):
    name = "init"
    help = "ask the hub's Curator to configure the project's graph from its data profile"
    hub_errors = True
    arguments = WITH_PROJECT

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.hub.cli.kg.init import line_of
        from evo_agents.hub.client import Hub, load_credentials
        from evo_agents.hub.kg_routes import InitRuns

        project = ctx.project
        credentials = load_credentials()
        found = InitRuns(Hub(credentials.url, credentials.token), project.name).start()
        return shown(ctx, found, Text(f"kg init run of project {project.name}: {line_of(found or {})}"))


INIT = KgInit()
