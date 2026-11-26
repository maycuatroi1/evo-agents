"""``evo-agents worker attach N``: this terminal on the tmux session of run N (``evo-run-N``) while a person drives
its agent, as ``tmux attach`` does."""

from __future__ import annotations

import os
import sys

from evo_agents.clikit import Arg, Result
from evo_agents.worker.cli.base import WorkerCommand, WorkerContext
from evo_agents.worker.home import WorkerStateError


class Attach(WorkerCommand):
    name = "attach"
    help = "put this terminal on the tmux session of an interactive run (evo-run-N), as tmux attach does"
    arguments = (Arg("run", type=int, help="the run's id, as the web and `evo-agents hub run list` show it"),)

    def run(self, ctx: WorkerContext) -> Result:
        from evo_agents.worker import interactive

        run_id = ctx.args.run
        record = next((item for item in ctx.home.load_runs() if item["id"] == run_id), {})
        socket = record.get("tmux_socket") if isinstance(record.get("tmux_socket"), str) else None
        tmux = interactive.Tmux.from_env(os.environ, socket=socket)
        if not tmux.available:
            raise WorkerStateError("tmux is not on PATH: interactive runs need it on the worker")
        name = interactive.session_name(run_id)
        if not tmux.has_session(name):
            raise WorkerStateError(
                f"run {run_id} has no terminal on this machine: tmux session {name} is not there. A run has one while "
                "it is interactive, after a takeover from the web or `evo-agents hub run takeover`."
            )
        argv, env = tmux.attach_command(name, os.environ)
        sys.stdout.flush()
        os.execvpe(argv[0], argv, env)
        return Result(None)  # pragma: no cover - execvpe does not return


ATTACH = Attach()
