"""Committing what a run's agent left, and telling the run's owner about a push.

The daemon commits what the agent left uncommitted as ``run #N: <title>``, leaving out ``.evo-run/``, what hooks wrote
(``gitops.RUN_COMMIT_EXCLUDES``) and copies of hub plans as the hub wrote them, which a ``system`` event names
(``gitops.commit_run``). Each push is never forced and never merged; ``gitops.push`` refuses a default branch the plan
does not name before every push.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.worker import gitops
from evo_agents.worker.hubapi import Backoff, HubProblem, Unreachable
from evo_agents.worker.runner.common import HARD_STOP_TRIES, log, wait_or

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run

MAX_LEFT_OUT_NAMED = 20  # files a note of what a commit left out names in its text
MAX_LEFT_OUT_LISTED = 200  # and lists in its body


async def commit(run: Run, path: Path, repo: str | None = None) -> bool:
    """Commit what the agent left in the work tree at ``path`` as ``run #N: <title>``, and say in the run's log
    which files the commit leaves out (``gitops.commit_run``); whether a commit was made."""
    message = f"run #{run.id}: {run.title}"
    committed = await gitops.commit_run(path, message)
    extra = {"repo": repo} if repo else {}
    where = f" in {repo}" if repo else ""
    if committed.left_out:
        left = committed.left_out
        shown = ", ".join(left[:MAX_LEFT_OUT_NAMED])
        more = f" and {len(left) - MAX_LEFT_OUT_NAMED} more" if len(left) > MAX_LEFT_OUT_NAMED else ""
        run.note(
            f"Left out of the commit{where}, as what a hook or a plan export wrote rather than the agent's work: "
            f"{shown}{more}.",
            left_out=list(left[:MAX_LEFT_OUT_LISTED]),
            **extra,
        )
    if committed.made:
        run.note(f"Committed what the agent left{where or ' uncommitted'} as {message}.", **extra)
    return committed.made


async def send_notice(run: Run, body: dict, what: str) -> None:
    """Send the run's owner the notice ``body``, about ``what``; one the hub does not take is logged."""
    backoff = Backoff()
    for _ in range(HARD_STOP_TRIES + 1):
        try:
            await run.daemon.hub.notice(run.id, body)
            run.note(f"Notified the owner of {what}.")
            return
        except Unreachable:
            await wait_or(run.daemon.hard_stop, backoff.next())
        except HubProblem as exc:
            log.warning("notice not sent", extra={"run_id": run.id, "error": str(exc)})
            break
    run.note(f"The notice of {what} was not sent.")
