"""``evo-agents worker list``: the workers of the hub this machine is signed in to, as GET /v1/workers lists them: the
caller's own and the live ones other members share with them, every worker for a hub admin (``docs/workers.md``,
Commands). One line each says what an owner would otherwise ask each machine over ssh: whether it is online, the
evo-agents its daemon runs, whether it updates itself to the tag nightly, the runs it holds, the free disk of its
worktrees and each runtime's login, and its last heartbeat.
It needs ``hub login`` only, not a worker on this machine. ``worker_lines.describe`` is the line of one worker."""

from __future__ import annotations

from datetime import UTC, datetime

from evo_agents.clikit import Arg, Result, Text
from evo_agents.hub.client import Hub, load_credentials
from evo_agents.worker.cli.base import WorkerCommand, WorkerContext
from evo_agents.worker.worker_lines import describe


class List(WorkerCommand):
    name = "list"
    help = (
        "the workers of the hub this machine is signed in to, yours and those shared with you: status, "
        "evo-agents version, self-update, runs held, free disk, runtime logins, last heartbeat (needs `hub login`)"
    )
    arguments = (
        Arg("--revoked", action="store_true", help="also list revoked workers"),
        Arg("--json", action="store_true", help="the rows of GET /v1/workers as they are"),
    )

    def run(self, ctx: WorkerContext) -> Result:
        credentials = load_credentials()
        hub = Hub(credentials.url, credentials.token)
        workers = hub.call("GET", "/v1/workers?revoked=true" if ctx.args.revoked else "/v1/workers")
        if not workers:
            return Result(workers, Text(f"No worker of yours on {credentials.url}, and no member shares one with you."))
        nightly = next((worker["nightly"] for worker in workers if worker.get("nightly")), None)
        head = f"Workers on {credentials.url}"
        now = datetime.now(UTC)
        lines = [head + (f", nightly at {nightly['commit'][:7]}:" if nightly else ":")]
        return Result(workers, Text(*lines, *(f"  {describe(worker, now)}" for worker in workers)))


LIST = List()
