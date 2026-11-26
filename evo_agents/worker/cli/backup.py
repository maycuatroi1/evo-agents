"""``evo-agents worker backup --target WHAT --location WHERE [--made-at ISO] [--restore COMMAND]``: the command of a
run's agent, of any kind and runtime, that records a backup it made before deleting or overwriting data (plan
autonomy-levels, criterion a9, the floor S2).

It sends the backup to the hub with the worker's token (``POST /v1/worker/runs/{id}/backups``), which writes it to the
run's log and to the audit (``autonomy.backup``), then keeps it in the run's ``backups.jsonl`` on this worker, where the
hook of autonomy reads it (``autonomy_hook.fresh_backup``): a command of K9 runs only after a backup made within the
last 24 hours. ``--made-at`` is when the backup was made, with its time zone (now when absent); a time in the future is
refused. A failure is one ``error:`` line on stderr and exit status 1, and nothing is kept.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from evo_agents.clikit import Arg, Result, Text
from evo_agents.worker import autonomy_hook
from evo_agents.worker.cli.base import EXIT_FAILED, AgentCommand, AgentRunContext
from evo_agents.worker.home import WorkerStateError


def made_at(raw: str | None) -> datetime:
    """``--made-at`` as a time with its zone; now when absent; WorkerStateError for one that does not read."""
    if raw is None:
        return datetime.now(UTC)
    try:
        found = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        raise WorkerStateError(
            f"--made-at {raw!r} is not an ISO 8601 time, such as 2026-10-23T09:30:00+00:00"
        ) from None
    if found.tzinfo is None:
        raise WorkerStateError(f"--made-at {raw!r} needs its time zone, such as +00:00 or Z")
    if found > datetime.now(UTC) + autonomy_hook.SKEW:
        raise WorkerStateError(f"--made-at {raw!r} is in the future")
    return found


class Backup(AgentCommand):
    name = "backup"
    kinds = None  # a run of any kind
    hub_failed = EXIT_FAILED
    help = (
        "inside a run: record a backup you made before deleting or overwriting data; a command that deletes data for "
        "good (K9) runs only after a backup made within the last 24 hours"
    )
    arguments = (
        Arg("--target", required=True, help="what was backed up: a database, a table, a bucket, a path"),
        Arg("--location", required=True, help="where the backup is: a path, a URL, a snapshot's id"),
        Arg("--made-at", metavar="ISO", help="when it was made, with its time zone (default: now)"),
        Arg("--restore", metavar="COMMAND", help="the command that restores it"),
    )

    def run(self, ctx: AgentRunContext) -> Result:
        args = ctx.args
        ctx.check()
        run_id, home = ctx.run_id, ctx.home
        when = made_at(args.made_at)
        target, location = args.target.strip(), args.location.strip()
        if not target or not location:
            raise WorkerStateError("give what was backed up (--target) and where the backup is (--location)")
        body: dict = {"target": target, "location": location, "made_at": when.isoformat()}
        if args.restore:
            body["restore"] = args.restore
        config, token = ctx.config, ctx.token
        from evo_agents.worker import hubapi  # without the worker extra this fails as it always did

        async def send() -> dict:
            async with hubapi.new_session() as session:
                return await hubapi.WorkerHub(config.url, token, session).backup(run_id, body)

        try:
            answer = asyncio.run(send())
        except hubapi.HubProblem as exc:
            raise exc.hub_error() from None
        autonomy_hook.add_backup(autonomy_hook.backups_path(home, run_id), {**body, "seq": answer.get("seq")})
        fresh = autonomy_hook.fresh_backup(autonomy_hook.backups_path(home, run_id)) is not None
        said = f"Backup of {target} at {location}, made {when.isoformat()}, recorded for run #{run_id}" + (
            "" if fresh else "; it is older than 24 hours, so a command of K9 still needs a newer one"
        )
        return Result(answer, Text(said))


BACKUP = Backup()
