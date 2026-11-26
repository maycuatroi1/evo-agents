"""``evo-agents worker drain [--resume]``: the owner stops this worker claiming new runs, the runs it holds finishing,
or lets it claim them again, with the machine token of ``evo-agents hub login``."""

from __future__ import annotations

from evo_agents.clikit import Arg, Result, Text
from evo_agents.worker.cli.base import WorkerCommand, WorkerContext
from evo_agents.worker.membership import object_of, owner_hub


class Drain(WorkerCommand):
    name = "drain"
    help = "stop claiming new runs; the runs held finish (needs `hub login`)"
    arguments = (Arg("--resume", action="store_true", help="claim runs again"),)

    def run(self, ctx: WorkerContext) -> Result:
        config = ctx.home.load_config()
        action = "undrain" if ctx.args.resume else "drain"
        answer = object_of(owner_hub(config), "POST", f"/v1/workers/{config.worker_id}/{action}")
        status = answer.get("status")
        if ctx.args.resume:
            return Result(answer, Text(f"Worker {config.name} claims runs again (status {status})."))
        return Result(
            answer,
            Text(
                f"Worker {config.name} is draining: it claims no new run and finishes the {answer.get('held_runs', 0)} "
                f"it holds (status {status}). Undo with `evo-agents worker drain --resume`."
            ),
        )


DRAIN = Drain()
