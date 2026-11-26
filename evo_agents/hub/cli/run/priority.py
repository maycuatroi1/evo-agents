"""``evo-agents hub run priority RUN LEVEL``, for ``evo_agents.hub.cli.run``: change the priority of a queued run of the
project, a writer of it (POST /v1/projects/{p}/runs/{id}/priority). A worker claims its queued runs by priority, urgent
first and low last, then the oldest; a run already held keeps its slot. The run's log says who changed it. A refusal
(403 below writer, 404, 409 for a run no longer queued) prints the hub's reason and exits 1. Standard library only,
like the rest of the client.
"""

from __future__ import annotations

from evo_agents.clikit import Arg, Result, Text
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext
from evo_agents.hub.cli.run.keys import RUN_OUTPUT
from evo_agents.hub.cli.run.options import RUN_ID
from evo_agents.hub.cli.run.resources import Runs
from evo_agents.hub.runs import PRIORITIES


class RunPriority(HubCommand):
    name = "priority"
    help = "change the priority of a queued run, which a worker claims its queued runs by (the writer role)"
    arguments = (
        RUN_ID,
        PROJECT,
        Arg("level", metavar="LEVEL", choices=PRIORITIES, help=f"one of {', '.join(PRIORITIES)}"),
    )
    output = RUN_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        run = Runs.of(ctx).priority(ctx.args.run, ctx.args.level)
        return Result(run, Text(f"Run #{run['id']} is {run['state']} with priority {run['priority']}."))
