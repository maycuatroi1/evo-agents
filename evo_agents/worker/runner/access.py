"""What a run may reach: the leases it takes for its repos, and the preflight that checks them before its agent starts.

Once its leases are taken, and before anything is fetched or the agent starts, a run checks what it needs
(``preflight``): the project lists an origin for each of its repos, git reads it (and pushes to it, for a repo the run
pushes) with what the run holds, and each program its verify commands call is on the run's PATH. A check that does not
pass fails the run before the agent starts, with its cause (the report's ``failure_cause``: origin, credentials or
missing_tool).

The leases stay in the daemon's memory (``credentials.RunCredentials``): the run's git gets them through
``credentials.ticketed``, its agent through ``Agent.env``, each event goes through ``credentials.scrub`` before the
spool, and they are given back once the run ends here, whether it ended, was parked, or the daemon stops. A push of the
daemon that fails to authenticate on a leased origin takes the leases again once and pushes once more
(``RunCredentials.with_renewal``). The agent of a run of the Curator never holds a push credential
(``credentials.RunCredentials.guarded``): only the daemon's own git gets the run's leases, through a ticket of one
command.
"""

from __future__ import annotations

import contextlib
from collections.abc import Collection, Iterable
from typing import TYPE_CHECKING

from evo_agents.worker import gitops, preflight
from evo_agents.worker.runner.common import RunFailed, cut
from evo_agents.worker.runner.transitions import MAX_ERROR_CHARS

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run


def repo_names(repos: Iterable) -> list[str]:
    """The names among ``repos``, each once, in order."""
    return list(dict.fromkeys(repo for repo in repos if isinstance(repo, str) and repo))


async def take_credentials(run: Run, repos: Iterable) -> None:
    """Take the run's leases for ``repos``, handed to the origins of their checkouts here."""
    origins: dict[str, list[str]] = {}
    for name in repo_names(repos):
        checkout = run.daemon.checkout_for(run.project, name)
        if checkout is not None:
            with contextlib.suppress(gitops.GitError, OSError):
                origins[name] = await gitops.remote_urls(checkout)
    await run.credentials.take(run.daemon.hub, origins, stop=run.daemon.hard_stop)


def run_path(run: Run) -> str | None:
    """The PATH the run's verify commands run with: the agent's."""
    return run.agent.env().get("PATH")


async def check(
    run: Run,
    repos: Iterable,
    *,
    path: str | None,
    pushes: Collection[str] = (),
    verify: Collection[str] = (),
    hidden: list[str] | None = None,
) -> None:
    """The preflight of the run (``preflight``): each of ``repos`` has an origin in the project and one git reads with
    what the run holds, and pushes to for those of ``pushes``; each program ``verify`` and ``hidden`` (a judge run's
    hidden checks) call is on ``path``. RunFailed with every problem found, the cause of the first: the agent does not
    start."""
    names = repo_names(repos)
    problems: list[preflight.Problem] = []
    unlisted = run.credentials.unlisted
    for name in names:
        if name in unlisted:
            problems.append(
                preflight.Problem(
                    "origin",
                    f"project {run.project} lists no origin for {name}, so no credential is leased for it: add "
                    "the repo with its origin to the harness and run `evo-agents hub project register` again",
                )
            )
    for name in names:
        if name not in unlisted:
            found = await reach(run, name, push=name in pushes)
            if found is not None:
                problems.append(found)
    problems += preflight.verify_problems(verify, path)
    problems += preflight.hidden_problems(hidden or [], path)
    if problems:
        for problem in problems:
            run.note(f"Preflight: {problem.message}.", cause=problem.cause)
        raise RunFailed(cut(preflight.summary(problems), MAX_ERROR_CHARS), cause=problems[0].cause)
    checked = [f"{len(names)} repo(s)"]
    if verify or hidden:
        checked.append(f"the programs of {len(verify) + len(hidden or [])} command(s)")
    run.note(f"Preflight passed: {' and '.join(checked)}.")


async def reach(run: Run, name: str, *, push: bool) -> preflight.Problem | None:
    """The problem git has reading the origin of ``name`` (and pushing to it, with ``push``) with what the run holds,
    or None; a repo without a checkout or an origin here is left to the steps after, which say so."""
    checkout = run.daemon.checkout_for(run.project, name)
    urls = run.credentials.origins.get(name) or []
    if checkout is None or not urls or all(preflight.is_local(url) for url in urls):
        return None
    covered = run.credentials.covers(name)
    if push and run.curator is not None and not covered:
        return preflight.Problem(
            "credentials",
            f"{name}: a run of the Curator pushes only with the credential the hub leased it, and no lease covers "
            f"{urls[0]}: this machine's own credentials are not used",
        )
    with run.credentials.ticketed(run.daemon.env) as env:
        refused = await preflight.can_reach(checkout, run.id, push=push, env=env)
    if refused is None:
        return None
    what, said = refused
    verb = "read" if what == "read" else "push to"
    if covered:
        why = "with the credential the hub leased the run"
    else:
        why = f"with this machine's own credentials (the hub leased none: {run.credentials.unleased.get(name)})"
        if name not in run.credentials.unleased:
            why = "with this machine's own credentials (no lease of the run covers it)"
    return preflight.Problem("credentials", f"git cannot {verb} {name} at {urls[0]} {why}: {said}")
