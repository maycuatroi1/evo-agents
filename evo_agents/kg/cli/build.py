"""``evo-agents kg build``: ask the hub for a build of the project's graph and wait until it has finished. Graphs are
built and read on the hub only (``evo_agents.hub.kg_hub_sync.HubSync.build``): no command reads a graph store left on
this machine."""

from __future__ import annotations

from evo_agents.clikit import Arg, Result, Text
from evo_agents.kg.cli.base import WITH_PROJECT, KgCommand, KgContext, shown
from evo_agents.kg.cli.sync import syncer


class KgBuild(KgCommand):
    name = "build"
    help = "ask the hub for a build of the project's graph and wait for it (the writer role)"
    hub_errors = True  # signing in, before the build is asked
    arguments = (*WITH_PROJECT, Arg("--timeout", type=float, help="seconds to wait at most (default: 1800)"))

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.hub.kg_hub_sync import BUILD_WAIT

        project = ctx.project
        build = syncer(project).build(timeout=ctx.args.timeout or BUILD_WAIT)
        return shown(
            ctx, {"project": project.name, **build.to_json()}, Text(build.summary_line()), 0 if build.ok else 1
        )


BUILD = KgBuild()
