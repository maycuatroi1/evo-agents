"""``evo-agents worker env --run N``: the variables run N's leases add to its agent's environment, as export lines,
for the script of an interactive pane to evaluate (``credential_commands``). It prints nothing of a run that holds no
socket here, and needs the core package only."""

from __future__ import annotations

import sys

from evo_agents.clikit import Result
from evo_agents.worker.cli.base import WorkerCommand, WorkerContext
from evo_agents.worker.cli.git_credential import RUN


class Env(WorkerCommand):
    name = "env"
    help = "print what a run's leases add to its agent's environment, as export lines for a shell to eval"
    arguments = (RUN,)

    def run(self, ctx: WorkerContext) -> Result:
        from evo_agents.worker import credential_commands

        return Result(None, status=credential_commands.print_env(ctx.args.run, ctx.out, sys.stderr))


ENV = Env()
