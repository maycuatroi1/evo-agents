"""``evo-agents kg profile``: the data profile of the project's newest graph on the hub (``evo-agents hub kg
profile``)."""

from __future__ import annotations

from evo_agents.clikit import Arg, Result
from evo_agents.kg.cli.base import WITH_PROJECT, KgCommand, KgContext, done, on_hub

SINK = Arg("--sink", help="sink whose clearance applies (default: the agent session's)")


class KgProfile(KgCommand):
    name = "profile"
    help = "the data profile of the graph: what the corpus is made of, counts and shapes"
    arguments = (*WITH_PROJECT, SINK)

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.hub.cli.kg.profile import PROFILE

        return done(on_hub(ctx.args, PROFILE, sink=ctx.args.sink))


PROFILE = KgProfile()
