"""``evo-agents worker git-credential --run N get|store|erase``: git's credential helper for run N, which the run's
git configuration names; ``get`` answers from the run's leases through its socket (``credential_commands``),
``store`` and ``erase`` do nothing. It needs the core package only."""

from __future__ import annotations

import sys

from evo_agents.clikit import Arg, Result
from evo_agents.worker.cli.base import WorkerCommand, WorkerContext

RUN = Arg("--run", type=int, required=True, help="the run's id")


class GitCredential(WorkerCommand):
    name = "git-credential"
    help = (
        "git's credential helper for a run of this worker: get answers from the run's leases; store and erase "
        "do nothing"
    )
    arguments = (RUN, Arg("action", help="get, store or erase, as git calls its helper"))

    def run(self, ctx: WorkerContext) -> Result:
        from evo_agents.worker import credential_commands

        status = credential_commands.git_credential(ctx.args.run, ctx.args.action, sys.stdin, ctx.out, sys.stderr)
        return Result(None, status=status)


GIT_CREDENTIAL = GitCredential()
