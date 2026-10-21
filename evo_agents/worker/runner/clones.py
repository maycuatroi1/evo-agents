"""The repos of a run this worker has no checkout of, cloned before the run goes on (``ensure``), so a run never waits
or fails for want of a checkout a person would have to make by hand.

The hub hands a run to a daemon of ``runs.CLONES_CHECKOUTS`` whatever checkouts it has. Once the run's leases are taken
(``access.take_credentials``), each repo of the run with no checkout here is set up, several at a time
(``parallel.each``), under the lock of where it goes (``WorkerDaemon.repo_lock``), so two runs never clone one repo at
once:

- where: the path the hub registered for the repo, placed in the project's workspace as ``evo-agents hub registry
  pull`` places it, else under the worker's home (``checkouts.clone_place``);
- a git work tree there already, one a person cloned, or another run of this worker, is taken as it is, whatever its
  origin says (a fork or a mirror is still the repo the project names); nothing is cloned again;
- else the origin is the one the hub's answer to the leases names (the run's project's, or another project's of the
  run's owner), else the one config.json keeps of the project's repos; a repo without one fails the run with cause
  origin, saying so;
- a path taken by something that is no work tree moves the clone under the worker's home instead;
- the clone goes through the run's leases (``RunCredentials.ticketed``), its origin handed them first
  (``RunCredentials.reach``); a clone git cannot make fails the run, with cause credentials when the remote refused
  the credential, else checkout. A chat run or a task run (``best_effort``) goes on without the repo instead, noted.

Each checkout set up is the daemon's from then on (``WorkerDaemon.add_checkout``): in its checkouts, which the next
heartbeat reports, and in ``checkouts`` of config.json.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.worker import checkouts, gitops
from evo_agents.worker.runner import parallel
from evo_agents.worker.runner.common import RunFailed

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run


async def ensure(run: Run, repos: Iterable, *, best_effort: bool = False) -> None:
    """Set up a checkout of each of ``repos`` this worker has none of (see the module's docstring); with
    ``best_effort``, one that cannot be set up is noted and left out, for a run that goes on without it (a chat run,
    a task run), rather than failing the run."""
    names = list(dict.fromkeys(name for name in repos if isinstance(name, str) and name))
    missing = [name for name in names if run.daemon.checkout_for(run.project, name) is None]
    if not missing:
        return
    run.note(f"No checkout of {', '.join(missing)} on this worker: setting each one up before the run goes on.")

    async def one(name: str) -> None:
        try:
            await _one(run, name)
        except RunFailed as exc:
            if not best_effort:
                raise
            run.note(f"No checkout of {name} could be set up: {exc.error}. The run goes on without it.", repo=name)

    await parallel.each(run, missing, one)


def _source(run: Run, name: str) -> dict:
    """Where repo ``name`` comes from: the hub's answer to the leases, else what config.json keeps of the project."""
    found = dict(run.credentials.sources.get(name) or {})
    for entry in (run.daemon.config.repos.get(run.project) or {}).get("repos") or []:
        if isinstance(entry, dict) and entry.get("name") == name:
            found["path"] = found.get("path") or entry.get("path")
            found["origin"] = found.get("origin") or entry.get("origin")
    return found


async def _one(run: Run, name: str) -> None:
    daemon = run.daemon
    source = _source(run, name)
    path = source.get("path") if isinstance(source.get("path"), str) else None
    target = await asyncio.to_thread(checkouts.clone_place, daemon.config, run.project, name, path, daemon.home.root)
    async with daemon.repo_lock(target):
        if daemon.checkout_for(run.project, name) is not None:  # another run set it up meanwhile
            return
        if checkouts.is_work_tree(target):
            run.note(f"Found a checkout of {name} at {target}: the run uses it.", repo=name)
            await _adopt(run, name, target)
            return
        origin = source.get("origin") if isinstance(source.get("origin"), str) else None
        if not origin:
            raise RunFailed(
                f"this worker has no checkout of {run.project}/{name}, and neither project {run.project} nor another "
                "project of the run's owner lists an origin to clone it from: add the repo with its origin to the "
                "harness and run `evo-agents hub project register` again",
                cause="origin",
            )
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            elsewhere = daemon.home.root / "checkouts" / run.project / name
            run.note(f"{target} is taken by something that is no checkout of {name}: cloning into {elsewhere}.")
            target = elsewhere
            if checkouts.is_work_tree(target):
                await _adopt(run, name, target)
                return
        elif target.exists():
            target.rmdir()  # an empty directory: the clone takes its place
        run.note(f"Cloning {name} from {origin} into {target}.", repo=name, origin=origin)
        run.credentials.reach(name, [origin])  # the clone goes through the run's leases for its origin
        try:
            with run.credentials.ticketed(daemon.env) as env:
                await gitops.clone(origin, target, env=env)
        except gitops.GitError as exc:
            cause = "credentials" if isinstance(exc, gitops.GitAuthError) else "checkout"
            raise RunFailed(f"cloning {name} from {origin} into {target} failed: {exc}", cause=cause) from None
        except OSError as exc:
            raise RunFailed(f"cloning {name} into {target} failed: {exc}", cause="checkout") from None
        daemon.add_checkout(run.project, name, target)
        run.note(f"Cloned {name} into {target}: the worker keeps it as its checkout.", repo=name)


async def _adopt(run: Run, name: str, path: Path) -> None:
    """Take the work tree at ``path`` as the worker's checkout of ``name``, its origin handed the run's leases."""
    urls: list[str] = []
    with contextlib.suppress(gitops.GitError, OSError):
        urls = await gitops.remote_urls(path)
    if urls:
        run.credentials.reach(name, urls)
    run.daemon.add_checkout(run.project, name, path)
