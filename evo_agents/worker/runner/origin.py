"""A run's checkouts and their origin before its worktrees exist: fetching origin with the run's leases, the commit a
worktree starts from, the branch it works on, and the default branches it never pushes.

A worktree on a plan's branch starts from ``origin/<branch>`` when the remote has it (else the local branch, else the
remote's default branch). When that branch is checked out somewhere else, or has commits here that the start does not
hold, the worktree is on a side branch of the run instead (``evo-run/<run>``, or ``evo-run/<run>/<folder>`` in a plan
run's directory), and the push still goes to the plan's branch.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.worker import gitops
from evo_agents.worker.runner.common import RunFailed
from evo_agents.worker.runner.transitions import working_branch

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run

RUN_BRANCH = "evo-run/{id}"
RUN_REPO_BRANCH = "evo-run/{id}/{folder}"  # a plan run's worktree of a repo whose branch is checked out elsewhere


async def fetch(run: Run, checkout: Path) -> None:
    """Fetch origin in ``checkout`` with the run's credentials; RunFailed when git cannot, with the cause
    credentials when the remote refused it for want of a credential, else checkout."""
    run.note(f"Fetching origin in {checkout}.")
    try:
        with run.credentials.ticketed(run.daemon.env) as env:
            await gitops.fetch(checkout, env=env)
    except gitops.GitError as exc:
        cause = "credentials" if isinstance(exc, gitops.GitAuthError) else "checkout"
        raise RunFailed(f"git fetch in {checkout} failed: {exc}", cause=cause) from None


async def start_point(checkout: Path, refs: Iterable[str]) -> tuple[str | None, str | None]:
    """The first of ``refs`` the checkout has, and its commit; (None, None) when it has none."""
    for ref in refs:
        base = await gitops.rev(checkout, ref)
        if base is not None:
            return ref, base
    return None, None


async def side_or_branch(checkout: Path, branch: str, base: str, side: str) -> tuple[str, str | None]:
    """The local branch a worktree of ``checkout`` starting at ``base`` works on: ``branch``, or ``side`` when
    ``branch`` is checked out in another worktree or has commits here that ``base`` lacks; and why it is ``side``."""
    in_use = await gitops.branches_in_worktrees(checkout)
    local = await gitops.rev(checkout, f"refs/heads/{branch}")
    fits = branch not in in_use and (local is None or await gitops.is_ancestor(checkout, local, base))
    return working_branch(branch, fits, in_use, side)


def protected_branches(remote_head: str | None, hub_default: str | None) -> set[str]:
    """The default branches of a repo, which a run never pushes unless its plan names one: the remote's HEAD, the
    hub's default_branch, main and master."""
    return {name for name in (remote_head, hub_default, *gitops.PROTECTED) if name}
