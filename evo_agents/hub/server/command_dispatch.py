"""Queueing a command run (``evo_agents.hub.command_runs``): the shell command line of a cron job, which a worker of its
owner, or one shared with them, runs with the machine's shell and no agent.

``queue_command_run`` inserts the run for the owner through ``dispatch.insert_run``, so it takes the project's default
profile, its principal and autonomy snapshot as any run of the owner, and an archived project or a stop of autonomy
refuses it. The run asks for no runtime and no model, has one attempt and no budget, and the claim gives it to a
daemon that says it runs command runs (``claim_rules``); a worker set to take only runs dispatched from the web takes
it only when its ``via`` is ``web``. ``command_workers`` counts the workers that could take one now, for the caller to
refuse a job no worker runs.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import command_runs
from evo_agents.hub.server.runs.service import dispatch, worker_fit


async def queue_command_run(
    conn: AsyncConnection,
    *,
    project_id: int,
    owner_id: int,
    command: str,
    repo: str | None = None,
    timeout_min: int = command_runs.DEFAULT_TIMEOUT_MINUTES,
    via: str = "machine",
    title: str | None = None,
    **columns,
) -> int:
    """Queue a command run of ``command`` for ``owner_id`` in project ``project_id``, in a worktree of ``repo`` at its
    default branch, or in an empty directory when None; its id. ValueError when the command or the timeout is not
    one a command run takes (``command_runs.command_problem``, ``timeout_problem``). ``via`` is its dispatched_via:
    ``machine`` by default, which a worker of ``dispatch_from = web`` never takes (``schedule`` names a schedule of the
    Curator, runs_schedule_check); ``columns`` are more columns of runs for the caller to set, such as the cron job
    the run fires for."""
    problem = command_runs.command_problem(command) or command_runs.timeout_problem(timeout_min)
    if problem is not None:
        raise ValueError(problem)
    values = {
        "kind": command_runs.KIND,
        "project_id": project_id,
        "title": title or command_runs.title_of("command", command),
        "dispatched_by": owner_id,
        "dispatched_via": via,
        "requested_runtime": "any",
        "runtime": "any",
        "mode": "headless",
        "approval": "auto",
        "timeout_s": timeout_min * 60,
        "max_attempts": command_runs.MAX_ATTEMPTS,
        "request": command,
        **columns,
    }
    if repo:  # none leaves the column NULL: a Python None would be JSON null, which runs_repos_check refuses
        values["repos"] = [{"repo": repo}]
    return await dispatch.insert_run(conn, values)  # its principal: the owner, bounded over its repo


async def command_workers(conn: AsyncConnection, project_id: int, owner_id: int, via: str = "machine") -> list[str]:
    """The names of the workers that could take a command run of ``owner_id`` in project ``project_id`` dispatched
    through ``via``: theirs and those shared with them there (``worker_fit.workers_of``), whose daemon says it runs
    command runs (``worker_fit.takes_kind``), a worker set to take runs dispatched from the web alone only when ``via``
    is ``web``."""
    found = await conn.execute(worker_fit.workers_of(owner_id, project_id))
    return [
        row.name
        for row in found
        if worker_fit.takes_kind(row, command_runs.KIND) and (row.dispatch_from != "web" or via == "web")
    ]
